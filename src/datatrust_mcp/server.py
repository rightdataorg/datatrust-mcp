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
from mcp.types import TextContent, Tool, ToolAnnotations

from . import config as cfg
from . import oauth
from ._gateway_tools_snapshot import GATEWAY_TOOLS_SNAPSHOT

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
#
# Naming decision (Phase 1):
#   Prefer gateway-canonical names from GET /api/mcp/v1/tools/list
#   (e.g. list_scenarios, run_dq_job) over the older client-side
#   datatrust_* prefixes for .NET-native tools. Python/FastAPI tools that
#   the gateway already exposes as datatrust_* / rightsight_* keep those
#   names unchanged. Client-local meta tools stay hard-coded below.
#
# Optional TOOL_NAME_ALIASES maps legacy MCP client names to gateway
# canonical names so older prompts/callers keep working.

# Always handled in this process — never forwarded to the gateway.
CLIENT_LOCAL_TOOL_NAMES = frozenset({
    "list_environments",
    "switch_default_environment",
    "datatrust_summarize_object_health",
})

# Legacy client names → gateway-canonical names (call_tool resolution only).
# list_tools advertises gateway-canonical names; aliases are not duplicated
# in the catalog unless a caller still invokes the old name.
TOOL_NAME_ALIASES: dict[str, str] = {
    "datatrust_list_scenarios": "list_scenarios",
    "datatrust_get_scenario": "get_scenario",
    "datatrust_run_scenario": "run_scenario",
    "datatrust_get_scenario_run_status": "get_scenario_run_status",
    "datatrust_get_scenario_exceptions": "get_scenario_exceptions",
    "datatrust_list_query_chains": "list_query_chains",
    "datatrust_get_query_chain": "get_query_chain",
    "datatrust_run_query_chain": "run_query_chain",
    "datatrust_get_query_results": "get_query_results",
    "datatrust_run_dq_job": "run_dq_job",
    "datatrust_get_dq_job_status": "get_dq_job_status",
}

_CATALOG_TTL_SEC = float(os.environ.get("DATATRUST_MCP_CATALOG_TTL", "300"))
_catalog_lock = asyncio.Lock()
# env_name -> (monotonic_deadline, tools_from_gateway)
_catalog_cache: dict[str, tuple[float, list[Tool]]] = {}
_last_good_catalog: list[Tool] | None = None


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
    schema = dict(schema or {})
    props = dict(schema.get("properties") or {})
    props["environment"] = _env_arg()
    schema["properties"] = props
    if "type" not in schema:
        schema["type"] = "object"
    return schema


def _tool_from_gateway_dict(entry: dict[str, Any]) -> Tool:
    """Build an MCP Tool from a gateway tools/list entry (or snapshot row)."""
    name = entry["name"]
    description = entry.get("description") or name
    schema = _augment_schema(entry.get("inputSchema") or {"type": "object", "properties": {}})
    annotations = None
    raw_ann = entry.get("annotations")
    if isinstance(raw_ann, dict) and raw_ann:
        try:
            annotations = ToolAnnotations(**{
                k: raw_ann[k]
                for k in (
                    "title", "destructiveHint", "idempotentHint",
                    "readOnlyHint", "openWorldHint",
                )
                if k in raw_ann
            })
        except Exception:
            annotations = None
    if annotations is not None:
        return Tool(name=name, description=description, inputSchema=schema, annotations=annotations)
    return Tool(name=name, description=description, inputSchema=schema)


def _static_gateway_tools() -> list[Tool]:
    """Offline / last-resort snapshot of gateway-canonical tools."""
    return [_tool_from_gateway_dict(row) for row in GATEWAY_TOOLS_SNAPSHOT]


CLIENT_LOCAL_TOOLS: list[Tool] = [
    Tool(
        name="list_environments",
        description=(
            "Show the DataTrust environments this MCP can reach (e.g. dev, qa, "
            "prod, demo). Also reports which env is the current default and "
            "which envs already have a valid signed-in session token cached. "
            "Use this first whenever the user asks about environments or you "
            "are unsure which env to target."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="switch_default_environment",
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
        },
    ),
    Tool(
        name="datatrust_summarize_object_health",
        description="[datatrust] Composite health report: score + failing rules + drift.",
        inputSchema=_augment_schema({
            "type": "object",
            "properties": {
                "objectName": {"type": "string"},
                "drift_days": {"type": "number", "default": 30},
            },
            "required": ["objectName"],
        }),
    ),
]


def merge_gateway_catalog(gateway_tools: list[Tool]) -> list[Tool]:
    """Prepend client-local tools and drop gateway rows that collide on name.

    This is the dynamic catalog path used by list_tools(); verify_tool_catalog
    asserts that this merge helper exists.
    """
    local_names = {t.name for t in CLIENT_LOCAL_TOOLS}
    merged = list(CLIENT_LOCAL_TOOLS)
    for tool in gateway_tools:
        if tool.name in local_names:
            continue
        merged.append(tool)
    return merged


def resolve_gateway_tool_name(name: str) -> str:
    """Map legacy client names to gateway-canonical names when needed."""
    return TOOL_NAME_ALIASES.get(name, name)


async def _http_get_gateway_tools(client: httpx.AsyncClient, env: cfg.Environment) -> list[Tool]:
    """GET {dotnet}/api/mcp/v1/tools/list and build Tool objects."""
    await _ensure_token(env)
    url = f"{env.dotnet_url}/api/mcp/v1/tools/list"

    async def _attempt() -> httpx.Response:
        return await client.get(
            url,
            headers=_auth_headers(env),
            follow_redirects=False,
        )

    try:
        resp = await _attempt()
    except httpx.RequestError as exc:
        raise RuntimeError(f"tools/list unreachable at {url}: {exc}") from exc

    if _looks_like_auth_failure(resp):
        _invalidate_session(env)
        await _ensure_token(env)
        try:
            resp = await _attempt()
        except httpx.RequestError as exc:
            raise RuntimeError(f"tools/list unreachable at {url}: {exc}") from exc

    if _looks_like_auth_failure(resp):
        raise RuntimeError(
            f"tools/list auth failure for '{env.label}' (HTTP {resp.status_code})"
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"tools/list returned {resp.status_code}: {resp.text[:300]}")

    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"tools/list non-JSON from '{env.label}'") from exc

    rows = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"tools/list missing tools[] from '{env.label}'")

    return [_tool_from_gateway_dict(row) for row in rows if isinstance(row, dict) and row.get("name")]


async def fetch_gateway_tools_for_env(env: cfg.Environment | None = None) -> list[Tool]:
    """Fetch (or return cached) gateway tools for an environment."""
    global _last_good_catalog
    if env is None:
        env = cfg.load_registry().get(None)

    now = asyncio.get_running_loop().time()
    cached = _catalog_cache.get(env.name)
    if cached and cached[0] > now:
        return list(cached[1])

    async with _catalog_lock:
        cached = _catalog_cache.get(env.name)
        now = asyncio.get_running_loop().time()
        if cached and cached[0] > now:
            return list(cached[1])

        async with httpx.AsyncClient(timeout=min(HTTP_TIMEOUT, 30.0), verify=oauth.verify_tls()) as client:
            tools = await _http_get_gateway_tools(client, env)
        _catalog_cache[env.name] = (now + _CATALOG_TTL_SEC, tools)
        _last_good_catalog = list(tools)
        return list(tools)


async def build_merged_tools(env: cfg.Environment | None = None) -> list[Tool]:
    """Live gateway catalog + client-local tools, with offline fallback.

    Preference order:
      1. Fresh / cached GET tools/list for the active env
      2. Last-good catalog from this process
      3. Embedded GATEWAY_TOOLS_SNAPSHOT (stdlib server still starts offline)
    """
    global _last_good_catalog
    try:
        gateway = await fetch_gateway_tools_for_env(env)
        return merge_gateway_catalog(gateway)
    except Exception as exc:
        print(
            f"[datatrust-mcp] WARNING: tools/list failed ({exc}); "
            "falling back to cached/static gateway catalog.",
            file=sys.stderr,
            flush=True,
        )
        if _last_good_catalog:
            return merge_gateway_catalog(list(_last_good_catalog))
        return merge_gateway_catalog(_static_gateway_tools())


# Backward-compatible name: static merge used when offline / for import-time
# inspection. Prefer build_merged_tools() at runtime.
TOOLS: list[Tool] = merge_gateway_catalog(_static_gateway_tools())


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[Tool]:
    return await build_merged_tools()


def _resolve_env(args: dict[str, Any]) -> tuple[cfg.Environment, dict[str, Any]]:
    """Pop `environment` from args, resolve via the registry."""
    args = dict(args or {})
    env_name = args.pop("environment", None)
    registry = cfg.load_registry()
    env = registry.get(env_name)
    return env, args


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Local-only meta tools never hit the gateway
    if name == "list_environments":
        result = await _list_environments()
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]
    if name == "switch_default_environment":
        result = await _switch_default(arguments or {})
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    env, args = _resolve_env(arguments)

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=oauth.verify_tls()) as client:
        if name == "datatrust_summarize_object_health":
            result = await _summarize(client, env, args)
            return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        # Everything else is a gateway passthrough. Resolve legacy aliases
        # to gateway-canonical names before the upstream call.
        gateway_name = resolve_gateway_tool_name(name)
        result = await _call_upstream(client, env, gateway_name, args)
        if isinstance(result, dict):
            result.setdefault("environment", env.name)
            if gateway_name != name:
                result.setdefault("resolved_tool", gateway_name)
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def _summarize(client: httpx.AsyncClient, env: cfg.Environment, args: dict[str, Any]) -> dict[str, Any]:
    object_name = args.get("objectName") or args.get("name")
    if not object_name:
        raise ValueError("datatrust_summarize_object_health requires objectName")
    drift_days = int(args.get("drift_days", 30))

    score_task = _call_upstream(client, env, "datatrust_get_quality_score", {"objectName": object_name})
    failed_task = _call_upstream(client, env, "datatrust_get_failed_rules", {"limit": 10})
    drift_task = _call_upstream(
        client, env, "rightsight_get_drift_events",
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
