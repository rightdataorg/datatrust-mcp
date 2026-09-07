"""Verify datatrust-mcp stdio tool catalog stays aligned with the gateway.

Phase 1 catalog model:
  - Gateway tools come from live GET /api/mcp/v1/tools/list (canonical names).
  - Client always keeps three local meta/composite tools.
  - server.py merges via merge_gateway_catalog / build_merged_tools.
  - Legacy datatrust_* native names may alias to gateway names at call time.

Usage:
    python scripts/verify_tool_catalog.py
    python scripts/verify_tool_catalog.py --url http://127.0.0.1:5000
    python scripts/verify_tool_catalog.py --offline
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "datatrust_mcp" / "server.py"
SNAPSHOT = ROOT / "src" / "datatrust_mcp" / "_gateway_tools_snapshot.py"

CLIENT_LOCAL = {
    "list_environments",
    "switch_default_environment",
    "datatrust_summarize_object_health",
}

# Dynamic catalog helpers that must exist in server.py
REQUIRED_SYMBOLS = {
    "merge_gateway_catalog",
    "build_merged_tools",
    "fetch_gateway_tools_for_env",
    "CLIENT_LOCAL_TOOLS",
    "CLIENT_LOCAL_TOOL_NAMES",
    "TOOL_NAME_ALIASES",
}


def _parse_server_symbols() -> tuple[set[str], set[str], set[str]]:
    """Return (defined_names, client_local_tool_literal_names, alias_keys)."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    defined: set[str] = set()
    local_tool_names: set[str] = set()
    alias_keys: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    defined.add(target.id)
                    if target.id == "CLIENT_LOCAL_TOOLS" and isinstance(node.value, ast.List):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "Tool":
                                for kw in elt.keywords:
                                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                        local_tool_names.add(kw.value.value)
                    if target.id == "TOOL_NAME_ALIASES" and isinstance(node.value, ast.Dict):
                        for key in node.value.keys:
                            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                                alias_keys.add(key.value)
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
            if node.target.id == "CLIENT_LOCAL_TOOLS" and isinstance(node.value, ast.List):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Call) and getattr(elt.func, "id", None) == "Tool":
                        for kw in elt.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                local_tool_names.add(kw.value.value)
            if node.target.id == "TOOL_NAME_ALIASES" and isinstance(node.value, ast.Dict):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        alias_keys.add(key.value)
    return defined, local_tool_names, alias_keys


def _source_has_dynamic_fetch() -> bool:
    src = SERVER.read_text(encoding="utf-8")
    return (
        "/api/mcp/v1/tools/list" in src
        and "merge_gateway_catalog" in src
        and "build_merged_tools" in src
        and "PASSTHROUGH" not in src  # hard-coded passthrough set retired
    )


def _load_snapshot_names() -> set[str]:
    tree = ast.parse(SNAPSHOT.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "GATEWAY_TOOLS_SNAPSHOT":
            data = ast.literal_eval(node.value)
            return {row["name"] for row in data}
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "GATEWAY_TOOLS_SNAPSHOT":
                    data = ast.literal_eval(node.value)
                    return {row["name"] for row in data}
    raise RuntimeError("GATEWAY_TOOLS_SNAPSHOT not found in snapshot module")


def _fetch_live_tools(base_url: str, api_key: str | None) -> list[dict]:
    url = base_url.rstrip("/") + "/api/mcp/v1/tools/list"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    tools = body.get("tools") if isinstance(body, dict) else None
    if not isinstance(tools, list):
        raise RuntimeError(f"Unexpected tools/list payload from {url}")
    return tools


def _redact_key(key: str | None) -> str:
    if not key:
        return "(none)"
    if key.startswith("dtmcp_"):
        return "dtmcp_<redacted>"
    return "<redacted>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("DATATRUST_GATEWAY_URL", "http://127.0.0.1:5000"),
        help="Gateway base URL (default DATATRUST_GATEWAY_URL or http://127.0.0.1:5000)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip live tools/list; only check source/snapshot invariants",
    )
    args = parser.parse_args(argv)

    errors: list[str] = []
    defined, local_names, alias_keys = _parse_server_symbols()

    missing_syms = REQUIRED_SYMBOLS - defined
    if missing_syms:
        errors.append(f"server.py missing dynamic-catalog symbols: {sorted(missing_syms)}")

    if local_names != CLIENT_LOCAL:
        errors.append(
            f"CLIENT_LOCAL_TOOLS mismatch: got {sorted(local_names)}, "
            f"expected {sorted(CLIENT_LOCAL)}"
        )

    if not _source_has_dynamic_fetch():
        errors.append(
            "server.py does not appear to fetch /api/mcp/v1/tools/list and merge "
            "via merge_gateway_catalog (or still depends on PASSTHROUGH)"
        )

    if "list_tools" not in defined or "call_tool" not in defined:
        errors.append("server.py must define list_tools and call_tool handlers")

    # Offline snapshot must use gateway-canonical names for natives that used
    # to be datatrust_*-prefixed on the client.
    try:
        snap_names = _load_snapshot_names()
    except Exception as exc:
        errors.append(f"snapshot load failed: {exc}")
        snap_names = set()

    if snap_names:
        if "list_scenarios" not in snap_names:
            errors.append("offline snapshot missing gateway-canonical list_scenarios")
        if "datatrust_list_scenarios" in snap_names:
            errors.append(
                "offline snapshot still uses legacy datatrust_list_scenarios; "
                "prefer gateway-canonical list_scenarios"
            )
        # Aliases should point at names present in snapshot when both exist
        for legacy, canon in (
            ("datatrust_list_scenarios", "list_scenarios"),
            ("datatrust_run_dq_job", "run_dq_job"),
        ):
            if legacy not in alias_keys:
                errors.append(f"TOOL_NAME_ALIASES missing {legacy}")
            if canon not in snap_names:
                errors.append(f"snapshot missing canonical target {canon}")

    live_names: set[str] = set()
    live_note = "skipped (--offline)"
    if not args.offline:
        api_key = os.environ.get("DATATRUST_API_KEY")
        try:
            live = _fetch_live_tools(args.url, api_key)
            live_names = {t.get("name") for t in live if isinstance(t, dict) and t.get("name")}
            live_note = f"{len(live_names)} tools from {args.url} (key={_redact_key(api_key)})"
            if "list_scenarios" not in live_names:
                errors.append("live tools/list missing list_scenarios")
            if not live_names:
                errors.append("live tools/list returned zero tools")
            # Snapshot should be a reasonable subset / match of live names
            if snap_names and live_names and not (snap_names <= live_names or live_names <= snap_names or len(snap_names & live_names) >= 20):
                errors.append(
                    f"snapshot and live catalog diverge too far "
                    f"(overlap={len(snap_names & live_names)})"
                )
        except Exception as exc:
            # Soft-fail live when offline gateway: still require source invariants
            live_note = f"FAILED ({exc})"
            errors.append(f"live tools/list failed: {exc}")

    print(f"Client-local tools:     {sorted(CLIENT_LOCAL)}")
    print(f"Dynamic symbols:        {sorted(REQUIRED_SYMBOLS & defined)}")
    print(f"Alias keys:             {len(alias_keys)}")
    print(f"Snapshot tools:         {len(snap_names)}")
    print(f"Live tools/list:        {live_note}")
    if live_names:
        print(f"  includes list_scenarios: {'list_scenarios' in live_names}")

    if errors:
        print("\nFAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("\nOK — dynamic catalog path present; client-local tools retained; "
          "gateway naming preferred.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
