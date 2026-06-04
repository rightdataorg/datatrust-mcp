"""stdio MCP server for DataTrust / RightSight — multi-environment.

The MCP server knows about every DataTrust environment the customer has
configured (dev/qa/prod/demo). Every tool call:

  * Optionally accepts an `environment` argument. If omitted, the
    registry's default is used. The user can flip the default with
    `switch_default_environment` and it persists across restarts.
  * On 401 from the FastAPI for that env, the MCP runs an OAuth flow
    against THAT env's .NET. A separate session token lives at
    ~/.config/datatrust-mcp/tokens/<env_name>.json.
  * Audit + rate limits + scope checks happen on the FastAPI side per
    env, as before.

Config lives at ~/.config/datatrust-mcp/environments.json. A fresh
install does this once with:

    datatrust-mcp setup <https://datatrust.customer/api/MCPInstall/Config>

See README.md for the deployment shape.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx
from dotenv import load_dotenv

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import config as cfg
from . import oauth

load_dotenv()

ENV_API_KEY = os.environ.get("DATATRUST_API_KEY")  # admin/CI bypass
if ENV_API_KEY and not os.environ.get("DATATRUST_MCP_ALLOW_SHARED_KEY"):
    print(
        "[datatrust-mcp] WARNING: DATATRUST_API_KEY bypasses per-user OAuth. "
        "Unset it for personal sessions.",
        file=sys.stderr,
        flush=True,
    )
HTTP_TIMEOUT = float(os.environ.get("DATATRUST_HTTP_TIMEOUT", "180"))
CLIENT_NAME = os.environ.get("DATATRUST_MCP_CLIENT_NAME", "claude-desktop")

server = Server("datatrust")

_session_lock = asyncio.Lock()
_session_tokens: dict[str, dict[str, Any]] = {}  # env_name -> token dict


# ---------------------------------------------------------------------------
# Per-env token resolution
# ---------------------------------------------------------------------------

def _current_token(env: cfg.Environment) -> str | None:
    """Resolve the API key we should send for this environment.
        1. DATATRUST_API_KEY env var (admin/CI bypass — used for all envs)
        2. In-memory token cache for this env
        3. Persisted token on disk for this env
    """
    if ENV_API_KEY:
        return ENV_API_KEY
    tok = _session_tokens.get(env.name)
    if tok and tok.get("access_token"):
        return tok["access_token"]
    persisted = oauth.load_token(env.name)
    if persisted:
        _session_tokens[env.name] = persisted
        return persisted.get("access_token")
    return None


def _auth_headers(env: cfg.Environment) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "x-mcp-client-name": CLIENT_NAME,
        "x-mcp-environment": env.name,   # informational; FastAPI can log it
    }
    tok = _current_token(env)
    if tok:
        headers["x-api-key"] = tok
    return headers


def _auth_mode() -> str:
    """Which OAuth flavor to use: 'device' (default) or 'loopback'.

    Device flow works on both HTTP and HTTPS DataTrust hosts and is the
    only flow that survives strict-HTTPS / HSTS deployments. Loopback is
    kept as an opt-in for users who prefer the auto-callback UX on a
    plain-HTTP dev box. Set DATATRUST_MCP_AUTH_MODE=loopback to switch.
    """
    return (os.environ.get("DATATRUST_MCP_AUTH_MODE") or "device").lower().strip()


async def _ensure_token(env: cfg.Environment) -> str:
    """Return a session token for `env`, running OAuth if needed."""
    tok = _current_token(env)
    if tok:
        return tok
    async with _session_lock:
        tok = _current_token(env)
        if tok:
            return tok
        loop = asyncio.get_running_loop()
        mode = _auth_mode()
        try:
            if mode == "loopback":
                token_data = await loop.run_in_executor(
                    None,
                    lambda: oauth.run_oauth_flow(
                        env.dotnet_url, env_name=env.name, env_label=env.label,
                    ),
                )
            else:
                token_data = await loop.run_in_executor(
                    None,
                    lambda: oauth.run_device_flow(
                        env.dotnet_url,
                        env_name=env.name,
                        env_label=env.label,
                        client_name=CLIENT_NAME,
                    ),
                )
        except Exception as exc:
            raise RuntimeError(
                f"DataTrust sign-in to '{env.label}' did not complete: {exc}. "
                f"Make sure {env.dotnet_url} is reachable and try again."
            )
        _session_tokens[env.name] = token_data
        return token_data["access_token"]


# ---------------------------------------------------------------------------
# Upstream call (per-env)
# ---------------------------------------------------------------------------

def _looks_like_auth_failure(resp: httpx.Response) -> bool:
    """True when the gateway redirected to login or returned a non-API body."""
    if resp.status_code in (401, 403):
        return True
    if resp.status_code in (301, 302, 303, 307, 308):
        return True
    if not resp.content or not resp.content.strip():
        return True
    ct = (resp.headers.get("content-type") or "").lower()
    if "text/html" in ct:
        return True
    return False


def _invalidate_session(env: cfg.Environment) -> None:
    _session_tokens.pop(env.name, None)
    oauth.clear_token(env.name)


def _parse_gateway_body(resp: httpx.Response, tool_name: str, env_label: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        snippet = (resp.text or "")[:200]
        raise RuntimeError(
            f"DataTrust gateway for '{env_label}' returned non-JSON (HTTP {resp.status_code}). "
            f"This usually means the server redirected to a login page — redeploy the latest "
            f"DataTrust build with MCP API routes enabled. Body starts with: {snippet!r}"
        ) from exc

    if body.get("isError"):
        err_text = body.get("content", [{}])[0].get("text", "Unknown upstream error")
        raise RuntimeError(f"Upstream tool error ({tool_name} on '{env_label}'): {err_text}")

    text_payload = body.get("content", [{}])[0].get("text", "{}")
    try:
        return json.loads(text_payload)
    except json.JSONDecodeError:
        return {"raw": text_payload}


async def _call_upstream(
    client: httpx.AsyncClient,
    env: cfg.Environment,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    await _ensure_token(env)
    # Goal: every MCP client call goes through the .NET gateway, never
    # the Python FastAPI directly. .NET validates the API key against
    # MCP_ApiKeys, resolves the DataTrust user, and forwards to FastAPI
    # via the trusted-backend channel. FastAPI is a private internal
    # service and shouldn't accept browser/MCP-client traffic.
    url = f"{env.dotnet_url}/api/mcp/v1/tools/call"

    async def _attempt() -> httpx.Response:
        return await client.post(
            url,
            headers=_auth_headers(env),
            json={"name": name, "arguments": arguments},
            follow_redirects=False,
        )

    try:
        resp = await _attempt()
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Could not reach DataTrust .NET gateway for env '{env.label}' at {url}. "
            f"Make sure the DataTrust web app is running and reachable. "
            f"Underlying error: {exc}"
        ) from exc

    if _looks_like_auth_failure(resp):
        _invalidate_session(env)
        await _ensure_token(env)
        try:
            resp = await _attempt()
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"Could not reach DataTrust .NET gateway for env '{env.label}' at {url}. "
                f"Underlying error: {exc}"
            ) from exc

    if _looks_like_auth_failure(resp):
        loc = resp.headers.get("location", "")
        hint = f" Redirected to {loc}." if loc else ""
        raise RuntimeError(
            f"DataTrust sign-in required for '{env.label}' (HTTP {resp.status_code}).{hint} "
            "Complete the browser login when prompted, then retry."
        )

    if resp.status_code >= 400:
        raise RuntimeError(f"Gateway returned {resp.status_code}: {resp.text[:500]}")

    return _parse_gateway_body(resp, name, env.label)


# ---------------------------------------------------------------------------
# Tool catalog
# ---------------------------------------------------------------------------

# Tools that just proxy to FastAPI. They all accept an optional `environment`.
PASSTHROUGH = {
    "search_metadata", "list_data_assets", "list_domains",
    "get_quality_score", "get_failed_rules", "get_run_history",
    "list_dq_jobs", "get_drift_events", "workspace_summary",
    "propose_scenarios", "answer_clarifications", "list_pending_scenarios",
    "confirm_and_create_scenarios", "list_connection_profiles",
    # .NET-native tools — implemented in the DataTrust gateway, not FastAPI.
    # The MCP client treats them like any other gateway passthrough.
    "list_scenarios", "get_scenario", "run_scenario",
    "get_scenario_run_status", "get_scenario_exceptions",
    "list_query_chains", "get_query_chain", "run_query_chain",
    "get_query_results",
    "run_dq_job", "get_dq_job_status",
}


def _env_arg() -> dict:
    """Standard `environment` arg shape — added to every tool's inputSchema."""
    return {
        "type": "string",
        "description": (
            "Which DataTrust environment to target (e.g. 'dev', 'qa', 'prod', "
            "'demo'). Defaults to the configured default. Use list_environments "
            "to see available choices."
        ),
    }


def _augment_schema(schema: dict) -> dict:
    schema = dict(schema)
    props = dict(schema.get("properties") or {})
    props["environment"] = _env_arg()
    schema["properties"] = props
    return schema


TOOLS: list[Tool] = [
    Tool(name="list_environments",
        description=(
            "Show the DataTrust environments this MCP can reach (e.g. dev, qa, "
            "prod, demo). Also reports which env is the current default and "
            "which envs already have a valid signed-in session token cached. "
            "Use this first whenever the user asks about environments or you "
            "are unsure which env to target."
        ),
        inputSchema={"type": "object", "properties": {}}),
    Tool(name="switch_default_environment",
        description=(
            "Change the default DataTrust environment for tool calls that "
            "don't pass an explicit `environment` argument. Persisted to disk "
            "so it survives restarts. Use when the user says 'switch to prod', "
            "'work in QA from now on', etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {"environment": _env_arg()},
            "required": ["environment"],
        }),
    Tool(name="search_metadata",
        description="Search DataTrust for data assets by keyword. Matches against asset name and description.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "number", "default": 10},
            },
            "required": ["query"],
        })),
    Tool(name="list_data_assets",
        description="List data assets, optionally filtered by domain or criticality.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "domain": {"type": "string"},
                "criticality": {"type": "string"},
                "limit": {"type": "number", "default": 10},
            },
        })),
    Tool(name="list_domains",
        description="List all business domains with asset counts.",
        inputSchema=_augment_schema({"type": "object", "properties": {}})),
    Tool(name="get_quality_score",
        description="Latest data-quality score for any DQ job/session matching the query.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"objectName": {"type": "string"}},
            "required": ["objectName"],
        })),
    Tool(name="get_failed_rules",
        description="Recent rule executions that failed or errored.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"limit": {"type": "number", "default": 10}},
        })),
    Tool(name="get_run_history",
        description="Recent DQ job-session executions.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "limit": {"type": "number", "default": 10},
            },
        })),
    Tool(name="list_dq_jobs",
        description="DataTrust DQ jobs with last-run status and schedule.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"limit": {"type": "number", "default": 10}},
        })),
    Tool(name="get_drift_events",
        description="Recent metadata-drift events.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "days": {"type": "number", "default": 30},
                "profileName": {"type": "string"},
                "limit": {"type": "number", "default": 10},
            },
        })),
    Tool(name="workspace_summary",
        description="At-a-glance overview of the DataTrust workspace.",
        inputSchema=_augment_schema({"type": "object", "properties": {}})),
    Tool(name="propose_scenarios",
        description="Auto-generate DataTrust reconciliation (FDR) scenarios from text or a file.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "requirements": {"type": "string"},
                "filePath": {"type": "string"},
            },
        })),
    Tool(name="answer_clarifications",
        description="Continue a scenario-generation session by answering questions.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "sessionId": {"type": "string"},
                "answers": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["sessionId", "answers"],
        })),
    Tool(name="list_pending_scenarios",
        description="Show current draft scenarios for a generation session.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"sessionId": {"type": "string"}},
            "required": ["sessionId"],
        })),
    Tool(name="confirm_and_create_scenarios",
        description="Deploy generated scenarios. Only call after explicit user approval.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "sessionId": {"type": "string"},
                "scenarios": {"type": "array", "items": {"type": "object"}},
                "folderId": {"type": "number"},
                "runInBackground": {"type": "boolean", "default": True},
            },
        })),
    Tool(name="list_connection_profiles",
        description="List active DataTrust connection profiles.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"limit": {"type": "number", "default": 50}},
        })),
    # ----- .NET-native: scenarios / FDR ------------------------------------
    Tool(name="list_scenarios",
        description="List DataTrust reconciliation (FDR/validation) scenarios. Optionally filter by name search or folder id.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "folderId": {"type": "number"},
                "limit": {"type": "number", "default": 25},
            },
        })),
    Tool(name="get_scenario",
        description="Get the definition of one scenario by id (header, type, thresholds, owner, latest session).",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"scenarioId": {"type": "number"}},
            "required": ["scenarioId"],
        })),
    Tool(name="run_scenario",
        description="Execute a scenario now. Returns the new session status. Only call after explicit user approval.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "scenarioId": {"type": "number"},
                "connectionId": {"type": "string"},
            },
            "required": ["scenarioId"],
        })),
    Tool(name="get_scenario_run_status",
        description="Recent execution sessions for a scenario, with status code and message.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "scenarioId": {"type": "number"},
                "limit": {"type": "number", "default": 10},
            },
            "required": ["scenarioId"],
        })),
    Tool(name="get_scenario_exceptions",
        description="Result/exception summary for a scenario's session(s) (status, message, pass/fail).",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "scenarioId": {"type": "number"},
                "sessionId": {"type": "number"},
            },
            "required": ["scenarioId"],
        })),
    # ----- .NET-native: query chains ---------------------------------------
    Tool(name="list_query_chains",
        description="List query chains in the DataTrust query builder. Optionally filter by name search.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "number", "default": 25},
            },
        })),
    Tool(name="get_query_chain",
        description="Get one query / query chain by id (name, profile, SQL text, type).",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {"queryId": {"type": "number"}},
            "required": ["queryId"],
        })),
    Tool(name="run_query_chain",
        description="Execute a query chain now. Returns the run status. Only call after explicit user approval.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "queryId": {"type": "number"},
                "connectionId": {"type": "string"},
            },
            "required": ["queryId"],
        })),
    Tool(name="get_query_results",
        description="Recent execution sessions / results for a query or query chain.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "queryId": {"type": "number"},
                "limit": {"type": "number", "default": 10},
            },
            "required": ["queryId"],
        })),
    # ----- .NET-native: data quality execution -----------------------------
    Tool(name="run_dq_job",
        description="Trigger a Data Quality job (submitted to the execution engine). Only call after explicit user approval.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "jobId": {"type": "number"},
                "connectionId": {"type": "string"},
            },
            "required": ["jobId"],
        })),
    Tool(name="get_dq_job_status",
        description="Status / latest run result of a Data Quality job. Pass runId for a specific run, else returns the summary.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "jobId": {"type": "number"},
                "runId": {"type": "number"},
            },
            "required": ["jobId"],
        })),
    Tool(name="summarize_dq_for_object",
        description="Composite health report: score + failing rules + drift.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "objectName": {"type": "string"},
                "drift_days": {"type": "number", "default": 30},
            },
            "required": ["objectName"],
        })),
]


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


def _resolve_env(args: dict[str, Any]) -> tuple[cfg.Environment, dict[str, Any]]:
    """Pop `environment` from args, resolve via the registry."""
    args = dict(args or {})
    env_name = args.pop("environment", None)
    registry = cfg.load_registry()
    env = registry.get(env_name)
    return env, args


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Local-only meta tools never hit the FastAPI
    if name == "list_environments":
        result = await _list_environments()
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    if name == "switch_default_environment":
        result = await _switch_default(arguments or {})
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    env, args = _resolve_env(arguments)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        if name in PASSTHROUGH:
            result = await _call_upstream(client, env, name, args)
            # Tag every response with the env it served so the LLM never has
            # to guess where the data came from.
            if isinstance(result, dict):
                result.setdefault("environment", env.name)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        if name == "summarize_dq_for_object":
            result = await _summarize(client, env, args)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        raise ValueError(f"Unknown tool: {name}")


async def _summarize(client: httpx.AsyncClient, env: cfg.Environment, args: dict[str, Any]) -> dict[str, Any]:
    object_name = args.get("objectName") or args.get("name")
    if not object_name:
        raise ValueError("summarize_dq_for_object requires objectName")
    drift_days = int(args.get("drift_days", 30))

    score_task = _call_upstream(client, env, "get_quality_score", {"objectName": object_name})
    failed_task = _call_upstream(client, env, "get_failed_rules", {"limit": 10})
    drift_task = _call_upstream(
        client, env, "get_drift_events",
        {"profileName": object_name, "days": drift_days, "limit": 10},
    )
    score, failed, drift = await asyncio.gather(
        score_task, failed_task, drift_task, return_exceptions=True,
    )

    def _maybe(v):
        return {"error": str(v)} if isinstance(v, Exception) else v

    return {
        "environment": env.name,
        "object": object_name,
        "score": _maybe(score),
        "recentFailingRules": _maybe(failed),
        "recentDrift": _maybe(drift),
    }


async def _list_environments() -> dict:
    reg = cfg.load_registry()
    out = []
    for name in reg.names():
        env = reg.environments[name]
        token = oauth.load_token(name)
        out.append({
            "name": name,
            "label": env.label,
            "dotnet_url": env.dotnet_url,
            "is_default": name == reg.default,
            "signed_in": bool(token and token.get("access_token")),
            "signed_in_as": (token or {}).get("user_email") if token else None,
            "note": (
                "No cached session — first data tool call opens browser for your DataTrust login."
                if not (token and token.get("access_token"))
                else "Session cached locally; gateway may still require re-login if expired."
            ),
        })
    return {
        "customer": reg.customer,
        "default_environment": reg.default,
        "environments": out,
        "config_source": reg.source,
    }


async def _switch_default(args: dict[str, Any]) -> dict:
    name = args.get("environment")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("`environment` argument is required")
    reg = cfg.set_default_environment(name.strip())
    return {
        "ok": True,
        "default_environment": reg.default,
        "available": reg.names(),
    }


def main() -> None:
    asyncio.run(_run_stdio())


async def _run_stdio() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    main()
