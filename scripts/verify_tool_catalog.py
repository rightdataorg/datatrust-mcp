"""Verify datatrust-mcp stdio tool catalog stays in sync with the gateway.

The DataTrust MCP gateway exposes:
  - Python/FastAPI tools via GET /mcp/tools/catalog (14 tools)
  - .NET-native tools via McpNativeTools.Catalog() (18 tools)
  - Plus 2 client-local meta tools and 1 client-local composite tool

This script ensures server.py TOOLS and PASSTHROUGH cover every gateway tool
so Cursor, Claude Desktop, VS Code Copilot, and Copilot Studio see the same names.

Usage:
    python scripts/verify_tool_catalog.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "src" / "datatrust_mcp" / "server.py"

# FastAPI catalog (ObservabilityPython/app/routers/mcp/catalog.py)
PYTHON_CATALOG = {
    "search_assets",
    "list_data_assets",
    "list_connections",
    "get_workspace_summary",
    "rightsight_list_domains",
    "rightsight_get_drift_events",
    "datatrust_get_quality_score",
    "datatrust_get_failed_rules",
    "datatrust_get_run_history",
    "datatrust_list_dq_jobs",
    "datatrust_propose_scenarios",
    "datatrust_answer_clarifications",
    "datatrust_list_pending_scenarios",
    "datatrust_confirm_and_create_scenarios",
}

# .NET McpNativeTools.Names (DataTrust/Services/Mcp/McpNativeTools.cs)
DOTNET_NATIVE = {
    "datatrust_list_scenarios",
    "datatrust_get_scenario",
    "datatrust_run_scenario",
    "datatrust_get_scenario_run_status",
    "datatrust_get_scenario_exceptions",
    "datatrust_list_query_chains",
    "datatrust_get_query_chain",
    "datatrust_run_query_chain",
    "datatrust_get_query_results",
    "datatrust_create_query_chain",
    "datatrust_get_source_columns",
    "datatrust_run_dq_job",
    "datatrust_get_dq_job_status",
    "datatrust_harvest_upload_document",
    "datatrust_harvest_extract",
    "datatrust_harvest_submit_clarifications",
    "datatrust_harvest_get_job",
    "datatrust_harvest_get_job_logs",
}

# Handled locally in server.py (never forwarded)
CLIENT_LOCAL = {
    "list_environments",
    "switch_default_environment",
    "datatrust_summarize_object_health",
}

GATEWAY_TOOLS = PYTHON_CATALOG | DOTNET_NATIVE


def _parse_server() -> tuple[set[str], set[str]]:
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    tool_names: set[str] = set()
    passthrough: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Tool":
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    tool_names.add(kw.value.value)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "PASSTHROUGH":
                    passthrough = {elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)}
    return tool_names, passthrough


def main() -> int:
    tools, passthrough = _parse_server()
    errors: list[str] = []

    missing_from_tools = (GATEWAY_TOOLS | CLIENT_LOCAL) - tools
    if missing_from_tools:
        errors.append(f"TOOLS missing: {sorted(missing_from_tools)}")

    extra_in_tools = tools - (GATEWAY_TOOLS | CLIENT_LOCAL)
    if extra_in_tools:
        errors.append(f"TOOLS unexpected extras: {sorted(extra_in_tools)}")

    missing_passthrough = GATEWAY_TOOLS - passthrough
    if missing_passthrough:
        errors.append(f"PASSTHROUGH missing gateway tools: {sorted(missing_passthrough)}")

    orphan_passthrough = passthrough - GATEWAY_TOOLS
    if orphan_passthrough:
        errors.append(f"PASSTHROUGH has unknown names: {sorted(orphan_passthrough)}")

    unhandled = tools - passthrough - CLIENT_LOCAL
    if unhandled:
        errors.append(f"TOOLS not in PASSTHROUGH or CLIENT_LOCAL: {sorted(unhandled)}")

    print(f"Gateway tools (Python + .NET): {len(GATEWAY_TOOLS)}")
    print(f"Client-local tools:          {len(CLIENT_LOCAL)}")
    print(f"server.py TOOLS:             {len(tools)}")
    print(f"server.py PASSTHROUGH:       {len(passthrough)}")

    if errors:
        print("\nFAILED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("\nOK — stdio client catalog matches gateway tool names.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
