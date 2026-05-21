"""`datatrust-mcp` admin/install CLI.

Goal: turn a fresh laptop into a working DataTrust MCP install with one
command. The customer admin generates a config in their DataTrust web UI
("Get MCP Installer") which produces a URL like
`https://datatrust.acme.local/api/MCPInstall/Config`. End user runs:

    datatrust-mcp setup https://datatrust.acme.local/api/MCPInstall/Config

This:
    1. Fetches the JSON manifest of all configured DataTrust envs.
    2. Writes ~/.config/datatrust-mcp/environments.json.
    3. Registers the MCP with every supported AI client found on the
       machine: Claude Desktop, Cursor, Claude Code (CLI).
    4. Prints a one-shot success report.

Subcommands:
    setup <url-or-file>      install/refresh
    status                   show registered envs + signed-in state
    envs                     same as status, JSON output
    uninstall                remove from AI clients (leaves token cache)
    logout [--env|--all]     revoke personal session + clear cached tokens
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import httpx

from . import config as cfg
from . import oauth


# ---------------------------------------------------------------------------
# Client adapters
# ---------------------------------------------------------------------------

SERVER_NAME = "datatrust"
HOME = Path.home()

# Default install binary path — resolved at setup time so `pip install`
# and `python -m datatrust_mcp setup` both register the correct launcher.
def _installed_binary() -> str:
    found = shutil.which("datatrust-mcp")
    if found:
        return found
    scripts_dir = Path(sys.executable).resolve().parent
    name = "datatrust-mcp.exe" if os.name == "nt" else "datatrust-mcp"
    candidate = scripts_dir / name
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError(
        "Could not locate the datatrust-mcp executable. "
        "Install first:  pip install git+https://github.com/rightdataorg/datatrust-mcp.git"
    )


def _claude_desktop_config() -> Path:
    # macOS
    p = HOME / "Library/Application Support/Claude/claude_desktop_config.json"
    if p.parent.exists():
        return p
    # Linux
    p2 = HOME / ".config/Claude/claude_desktop_config.json"
    return p2


def _cursor_config() -> Path:
    return HOME / ".cursor/mcp.json"


def _vscode_user_settings() -> Path:
    # VS Code / Copilot Chat MCP support via settings.json `github.copilot.mcp`
    return HOME / "Library/Application Support/Code/User/settings.json"


# --- Additional clients (added so one `setup` covers as many tools as
# possible). The list below is what we can reach via a stable on-disk
# config; UI-only clients (ChatGPT Desktop) just get printed instructions.

def _windsurf_config() -> Path:
    # Windsurf (Codeium / Cascade) — `~/.codeium/windsurf/mcp_config.json`.
    return HOME / ".codeium/windsurf/mcp_config.json"


def _gemini_cli_config() -> Path:
    # Google Gemini CLI — `~/.gemini/settings.json`, key `mcpServers`.
    return HOME / ".gemini/settings.json"


def _zed_config() -> Path:
    # Zed editor — `~/.config/zed/settings.json`, key `context_servers`.
    return HOME / ".config/zed/settings.json"


def _cline_config() -> Path:
    # Cline (VS Code extension) keeps its MCP list inside VS Code's
    # globalStorage. Path is `…/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`.
    return (HOME / "Library/Application Support/Code/User/globalStorage"
                  / "saoudrizwan.claude-dev/settings/cline_mcp_settings.json")


def _codex_cli_config() -> Path:
    # OpenAI Codex CLI — `~/.codex/config.toml`. TOML; we don't rewrite,
    # just emit a snippet for the user to paste.
    return HOME / ".codex/config.toml"


def _continue_config_yaml() -> Path:
    # Continue (VS Code / JetBrains plugin) — `~/.continue/config.yaml`.
    return HOME / ".continue/config.yaml"


def _chatgpt_desktop_dir() -> Path:
    # ChatGPT Desktop on macOS has no stable JSON config for MCP — the
    # "Apps" / "Developer mode" panel is UI-driven. We just check for
    # the app's presence so we can print useful instructions.
    return HOME / "Library/Application Support/com.openai.chat"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def _server_entry() -> dict:
    """The MCP server spec to insert into each client's config."""
    return {
        "command": _installed_binary(),
        "args": [],
        # No env vars needed — the binary reads environments.json from
        # ~/.config/datatrust-mcp at startup. Keeping env empty means
        # there's nothing for Claude Desktop's UI to silently rewrite.
        "env": {},
    }


def _install_into_claude(report: list[str]) -> None:
    p = _claude_desktop_config()
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Claude Desktop     {p}")


def _install_into_cursor(report: list[str]) -> None:
    p = _cursor_config()
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Cursor             {p}")


def _install_into_vscode(report: list[str]) -> None:
    p = _vscode_user_settings()
    if not p.exists():
        report.append(f"  (skip) VS Code       {p} not found")
        return
    data = _read_json(p)
    mcp = data.get("github.copilot.mcp.servers") or {}
    mcp[SERVER_NAME] = _server_entry()
    data["github.copilot.mcp.servers"] = mcp
    _write_json(p, data)
    report.append(f"  ✓ VS Code (Copilot)  {p}")


def _install_into_claude_code(report: list[str]) -> None:
    # The Claude Code CLI manages MCPs via `claude mcp add`. We invoke it
    # if available, falling back to writing the JSON directly.
    if shutil.which("claude"):
        try:
            subprocess.run(
                ["claude", "mcp", "add-json", "--scope", "user", SERVER_NAME,
                 json.dumps(_server_entry())],
                check=True, capture_output=True, text=True,
            )
            report.append(f"  ✓ Claude Code        registered via `claude mcp add-json`")
            return
        except subprocess.CalledProcessError as exc:
            report.append(f"  (warn) Claude Code   `claude mcp add-json` failed: {exc.stderr.strip()[:120]}")
    p = HOME / ".claude.json"
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Claude Code        {p}")


def _install_into_windsurf(report: list[str]) -> None:
    p = _windsurf_config()
    if not p.parent.parent.exists():
        report.append(f"  (skip) Windsurf       {p.parent.parent} not found — Windsurf not installed")
        return
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Windsurf           {p}")


def _install_into_gemini_cli(report: list[str]) -> None:
    p = _gemini_cli_config()
    # Gemini CLI creates ~/.gemini on first run; we can pre-create it.
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Gemini CLI         {p}")


def _install_into_zed(report: list[str]) -> None:
    p = _zed_config()
    if not p.parent.exists():
        report.append(f"  (skip) Zed            {p.parent} not found — Zed not installed")
        return
    data = _read_json(p)
    # Zed uses `context_servers` instead of `mcpServers`, and wraps the
    # command/args under a `command` sub-object.
    entry = {"command": {"path": _installed_binary(), "args": []}}
    data.setdefault("context_servers", {})[SERVER_NAME] = entry
    _write_json(p, data)
    report.append(f"  ✓ Zed                {p}")


def _install_into_cline(report: list[str]) -> None:
    p = _cline_config()
    if not p.parent.parent.exists():
        report.append(f"  (skip) Cline          extension globalStorage not found — install Cline in VS Code first")
        return
    data = _read_json(p)
    data.setdefault("mcpServers", {})[SERVER_NAME] = _server_entry()
    _write_json(p, data)
    report.append(f"  ✓ Cline              {p}")


def _install_into_codex_cli(report: list[str]) -> None:
    """OpenAI Codex CLI uses TOML — we don't rewrite, we print a snippet."""
    if not shutil.which("codex") and not _codex_cli_config().exists():
        report.append("  (skip) Codex CLI      `codex` not on PATH — install OpenAI Codex CLI first")
        return
    snippet = (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f'command = "{_installed_binary()}"\n'
        f"args = []\n"
    )
    report.append(
        f"  ⚠ Codex CLI         add this block to ~/.codex/config.toml manually:\n"
        f"{snippet.rstrip()}"
    )


def _install_into_continue(report: list[str]) -> None:
    """Continue (VS Code / JetBrains) — print YAML snippet."""
    if not _continue_config_yaml().parent.exists() and not (HOME / ".continue").exists():
        report.append("  (skip) Continue       ~/.continue not found — install Continue extension first")
        return
    snippet = (
        f"mcpServers:\n"
        f"  - name: {SERVER_NAME}\n"
        f"    command: {_installed_binary()}\n"
        f"    args: []\n"
    )
    report.append(
        f"  ⚠ Continue          add this to ~/.continue/config.yaml under `mcpServers`:\n"
        f"{snippet.rstrip()}"
    )


def _install_into_chatgpt_desktop(report: list[str]) -> None:
    """ChatGPT Desktop has no on-disk MCP config — UI-driven only."""
    if not _chatgpt_desktop_dir().exists():
        report.append("  (skip) ChatGPT       desktop app not detected")
        return
    report.append(
        "  ⚠ ChatGPT Desktop   MCP is UI-driven. Open ChatGPT → Settings →\n"
        "                       Connectors → 'Developer mode' → Add MCP server.\n"
        f"                       Type:    stdio\n"
        f"                       Command: {_installed_binary()}\n"
        f"                       Args:    (leave empty)\n"
        f"                       Name:    datatrust"
    )


def _install_microsoft_copilot_notice(report: list[str]) -> None:
    """Standalone Microsoft Copilot doesn't expose MCP yet."""
    # We don't bother probing — just leave a notice so users know.
    report.append(
        "  (info) MS Copilot     standalone Copilot app has no MCP integration yet;\n"
        "                        for Copilot in VS Code we already wrote settings.json above"
    )


CLIENT_ADAPTERS = [
    _install_into_claude,
    _install_into_cursor,
    _install_into_vscode,
    _install_into_claude_code,
    _install_into_windsurf,
    _install_into_gemini_cli,
    _install_into_zed,
    _install_into_cline,
    _install_into_codex_cli,
    _install_into_continue,
    _install_into_chatgpt_desktop,
    _install_microsoft_copilot_notice,
]


def _uninstall_from_clients(report: list[str]) -> None:
    for path_fn in (_claude_desktop_config, _cursor_config, _vscode_user_settings,
                    lambda: HOME / ".claude.json",
                    _windsurf_config, _gemini_cli_config, _zed_config, _cline_config):
        p = path_fn()
        if not p.exists():
            continue
        data = _read_json(p)
        # `mcpServers` covers Claude / Cursor / Windsurf / Gemini / Cline.
        # `github.copilot.mcp.servers` covers VS Code Copilot.
        # `context_servers` covers Zed.
        for key in ("mcpServers", "github.copilot.mcp.servers", "context_servers"):
            entries = data.get(key) or {}
            if SERVER_NAME in entries:
                entries.pop(SERVER_NAME, None)
                data[key] = entries
                _write_json(p, data)
                report.append(f"  ✓ removed datatrust from {p}")


# ---------------------------------------------------------------------------
# Manifest fetch
# ---------------------------------------------------------------------------

def _fetch_manifest(src: str) -> dict:
    """Accepts a URL or a local file path. Returns the parsed JSON manifest."""
    if src.startswith(("http://", "https://")):
        with httpx.Client(timeout=30.0, verify=False) as client:
            r = client.get(src)
        if r.status_code >= 400:
            raise RuntimeError(f"GET {src} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    p = Path(src).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"{src!r} is neither a URL nor an existing file")
    return json.loads(p.read_text())


def _validate_manifest(m: dict) -> dict:
    """Basic sanity check on the install JSON. Returns the cleaned manifest."""
    if not isinstance(m, dict):
        raise ValueError("manifest must be a JSON object")
    envs = m.get("environments") or {}
    if not isinstance(envs, dict) or not envs:
        raise ValueError("manifest has no 'environments' object")
    clean_envs = {}
    for name, body in envs.items():
        if not isinstance(name, str) or not isinstance(body, dict):
            continue
        fapi = (body.get("fastapi_url") or "").strip().rstrip("/")
        dnet = (body.get("dotnet_url") or "").strip().rstrip("/")
        if not dnet:
            continue
        if not fapi:
            fapi = dnet.replace(":5000", ":8000")
        clean_envs[name] = {
            "label": body.get("label") or name,
            "fastapi_url": fapi,
            "dotnet_url": dnet,
        }
    if not clean_envs:
        raise ValueError("manifest has no usable environments after validation")
    out = {
        "version": int(m.get("version") or 1),
        "customer": m.get("customer"),
        "default_environment": (
            m.get("default_environment")
            if m.get("default_environment") in clean_envs
            else next(iter(clean_envs.keys()))
        ),
        "environments": clean_envs,
    }
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_setup(args) -> int:
    print(f"[datatrust-mcp setup] fetching manifest from {args.source}")
    manifest = _validate_manifest(_fetch_manifest(args.source))
    saved = cfg.write_environments_file(manifest)
    print(f"  ✓ wrote {saved}")
    print(f"  ✓ customer={manifest.get('customer') or '(unset)'}, "
          f"envs=[{', '.join(manifest['environments'].keys())}], "
          f"default={manifest['default_environment']}")
    print()
    print("[datatrust-mcp setup] registering with AI clients")
    report: list[str] = []
    for adapter in CLIENT_ADAPTERS:
        try:
            adapter(report)
        except Exception as exc:
            report.append(f"  (skip) {adapter.__name__}: {exc}")
    for line in report:
        print(line)
    print()
    print("[datatrust-mcp setup] done")
    print(f"  Binary: {_installed_binary()}")
    print(f"  Config: {saved}")
    print(
        "\nNext step: restart Claude Desktop / Cursor / Copilot. Your first "
        "tool call per environment will open a browser for DataTrust sign-in. "
        "Tokens are then cached for 30 days."
    )
    return 0


def cmd_status(args) -> int:
    if os.environ.get("DATATRUST_API_KEY") and not os.environ.get("DATATRUST_MCP_ALLOW_SHARED_KEY"):
        print("WARNING: DATATRUST_API_KEY is set — all tool calls use a shared key, not your personal login.")
        print("         Unset it for per-user OAuth, or set DATATRUST_MCP_ALLOW_SHARED_KEY=1 for CI.\n")
    try:
        reg = cfg.load_registry()
    except RuntimeError as exc:
        print(f"(no install yet)\n  {exc}")
        return 1
    print(f"customer: {reg.customer or '(unset)'}")
    print(f"default:  {reg.default}")
    print(f"source:   {reg.source}")
    print()
    print(f"{'NAME':<10}  {'LABEL':<14}  {'SIGNED IN AS':<32}  ENDPOINT")
    print("-" * 100)
    for name in reg.names():
        env = reg.environments[name]
        tok = oauth.load_token(name)
        who = (tok or {}).get("user_email", "—") or "—"
        print(f"{name:<10}  {env.label:<14}  {who:<32}  {env.dotnet_url}")
    return 0


def cmd_logout(args) -> int:
    """Revoke cached session key(s) server-side and delete local tokens."""
    try:
        reg = cfg.load_registry()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    targets: list[str]
    if getattr(args, "all", False):
        targets = reg.names()
    elif args.env:
        targets = [args.env]
    else:
        targets = [reg.default]

    for name in targets:
        try:
            env = reg.get(name)
        except KeyError:
            print(f"  skip {name}: unknown environment", file=sys.stderr)
            continue
        tok = oauth.load_token(name)
        access = (tok or {}).get("access_token")
        who = (tok or {}).get("user_email") or "(unknown)"
        if access:
            url = f"{env.dotnet_url.rstrip('/')}/api/MCPAuth/Revoke"
            try:
                with httpx.Client(timeout=30.0, verify=False) as client:
                    r = client.post(url, headers={"x-api-key": access})
                if r.status_code >= 400:
                    print(f"  ! {name}: server revoke returned HTTP {r.status_code} ({who})")
                else:
                    print(f"  ✓ {name}: revoked server session for {who}")
            except httpx.RequestError as exc:
                print(f"  ! {name}: could not reach revoke endpoint: {exc}")
        else:
            print(f"  - {name}: no cached token")
        oauth.clear_token(name)
        print(f"  ✓ {name}: cleared local token cache")
    return 0


def cmd_envs(args) -> int:
    """Same as status, but JSON for piping into other tools."""
    try:
        reg = cfg.load_registry()
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    out = []
    for name in reg.names():
        env = reg.environments[name]
        tok = oauth.load_token(name)
        out.append({
            "name": name,
            "label": env.label,
            "fastapi_url": env.fastapi_url,
            "dotnet_url": env.dotnet_url,
            "is_default": name == reg.default,
            "signed_in_as": (tok or {}).get("user_email"),
        })
    print(json.dumps({"customer": reg.customer, "default": reg.default,
                      "environments": out}, indent=2))
    return 0


def cmd_uninstall(args) -> int:
    print("[datatrust-mcp uninstall] removing from AI clients")
    report: list[str] = []
    _uninstall_from_clients(report)
    for line in report:
        print(line)
    if not args.keep_config:
        try:
            cfg.ENV_CONFIG_PATH.unlink()
            print(f"  ✓ removed {cfg.ENV_CONFIG_PATH}")
        except FileNotFoundError:
            pass
    if args.purge_tokens:
        for p in (cfg.TOKENS_DIR.glob("*.json") if cfg.TOKENS_DIR.exists() else []):
            p.unlink()
            print(f"  ✓ removed token {p}")
        if oauth.LEGACY_TOKEN_PATH.exists():
            oauth.LEGACY_TOKEN_PATH.unlink()
            print(f"  ✓ removed token {oauth.LEGACY_TOKEN_PATH}")
    return 0


def cmd_activate(args) -> int:
    """Manual-paste fallback for the OAuth flow.

    When the browser-driven loopback callback fails (HTTPS host, strict
    CSP, mixed-content block, …) the DataTrust Success page shows the
    user a one-time auth code. They paste it back with:

        datatrust-mcp activate <env-name> <code>

    We POST it to <env>.dotnet_url/api/MCPAuth/Token, get the session
    key, and persist it to ~/.config/datatrust-mcp/tokens/<env>.json —
    same place the loopback path writes to. Next tool call uses it.
    """
    import time
    try:
        reg = cfg.load_registry()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        env = reg.get(args.env)
    except KeyError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    code = args.code.strip()
    if not code:
        print("ERROR: empty code", file=sys.stderr)
        return 2

    url = f"{env.dotnet_url}/api/MCPAuth/Token"
    print(f"[datatrust-mcp activate] exchanging code at {url}")
    with httpx.Client(timeout=30.0, verify=False) as client:
        try:
            r = client.post(url, json={"code": code})
        except httpx.RequestError as exc:
            print(f"ERROR: could not reach {url}: {exc}", file=sys.stderr)
            return 3
    if r.status_code >= 400:
        print(f"ERROR: token exchange failed (HTTP {r.status_code}): {r.text[:300]}",
              file=sys.stderr)
        return 4
    token = r.json()
    token["obtained_at"] = int(time.time())
    token["env_name"] = env.name
    oauth.save_token(token, env_name=env.name)
    print(f"  ✓ Signed in to {env.label} as "
          f"{token.get('user_email') or '(unknown user)'}")
    print(f"  ✓ Token saved to {oauth._token_path_for(env.name)}")
    print("\nReturn to your MCP client — the next tool call will use this session.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datatrust-mcp", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=False)

    p_setup = sub.add_parser("setup", help="install/refresh from manifest URL or file")
    p_setup.add_argument("source",
        help="URL like https://datatrust.acme/api/MCPInstall/Config OR a local manifest .json")
    p_setup.set_defaults(func=cmd_setup)

    p_status = sub.add_parser("status", help="show configured envs + signed-in state")
    p_status.set_defaults(func=cmd_status)

    p_envs = sub.add_parser("envs", help="JSON dump of configured envs (for scripting)")
    p_envs.set_defaults(func=cmd_envs)

    p_activate = sub.add_parser("activate",
        help="manual OAuth fallback: exchange a code from the DataTrust Success page")
    p_activate.add_argument("env", help="env name (e.g. dev, prod). See `datatrust-mcp status`.")
    p_activate.add_argument("code", help="the one-time code shown on the DataTrust Success page")
    p_activate.set_defaults(func=cmd_activate)

    p_uninstall = sub.add_parser("uninstall", help="remove MCP from AI clients")
    p_uninstall.add_argument("--keep-config", action="store_true",
                             help="keep ~/.config/datatrust-mcp/environments.json")
    p_uninstall.add_argument("--purge-tokens", action="store_true",
                             help="also delete cached session tokens")
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_logout = sub.add_parser("logout", help="revoke MCP session and clear cached tokens")
    p_logout.add_argument("env", nargs="?", default=None,
                          help="env name (default: current default env)")
    p_logout.add_argument("--all", action="store_true",
                          help="logout from every configured environment")
    p_logout.set_defaults(func=cmd_logout)

    args = ap.parse_args(argv)

    # If no subcommand AND no flags, fall back to stdio server mode (the
    # default invocation Claude Desktop uses). This keeps the existing
    # entry-point contract intact.
    if not args.command:
        from .server import main as run_server
        run_server()
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
