"""Smoke-test MCP tools against the configured default environment.

Usage:
    python scripts/smoke_test.py <search-keyword> [object-name]
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

from datatrust_mcp import config as cfg
from datatrust_mcp.server import _call_upstream, _summarize


async def run(keyword: str, obj: str | None) -> int:
    reg = cfg.load_registry()
    env = reg.get(None)
    failures = 0
    res = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"[1/3] search_metadata(query={keyword!r}) on env={env.name}")
        try:
            res = await _call_upstream(client, env, "search_metadata", {"query": keyword, "limit": 5})
            print(json.dumps(res, indent=2, default=str)[:800])
            print("    OK\n")
        except Exception as exc:
            print(f"    FAILED: {exc}\n")
            failures += 1

        target = obj
        if not target:
            try:
                first = (res.get("results") or [{}])[0]
                target = first.get("name")
            except Exception:
                target = None

        if not target:
            print("[2/3] get_quality_score: skipped (no object name and search returned nothing)")
            print("[3/3] summarize_dq_for_object: skipped\n")
            return failures

        print(f"[2/3] get_quality_score(objectName={target!r})")
        try:
            res = await _call_upstream(client, env, "get_quality_score", {"objectName": target})
            print(json.dumps(res, indent=2, default=str)[:800])
            print("    OK\n")
        except Exception as exc:
            print(f"    FAILED: {exc}\n")
            failures += 1

        print(f"[3/3] summarize_dq_for_object(objectName={target!r})")
        try:
            res = await _summarize(client, env, {"objectName": target, "drift_days": 30})
            print(json.dumps(res, indent=2, default=str)[:1200])
            print("    OK\n")
        except Exception as exc:
            print(f"    FAILED: {exc}\n")
            failures += 1

    return failures


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/smoke_test.py <search-keyword> [object-name]")
        sys.exit(2)
    keyword = sys.argv[1]
    obj = sys.argv[2] if len(sys.argv) > 2 else None
    failures = asyncio.run(run(keyword, obj))
    if failures:
        print(f"{failures} tool(s) failed.")
        sys.exit(1)
    print("All tools OK.")


if __name__ == "__main__":
    main()
