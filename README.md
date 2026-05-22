# datatrust-mcp

Local **stdio MCP client** for [DataTrust](https://getrightdata.com/) / **RightSight**. It runs on a user's laptop, authenticates through the customer's DataTrust **.NET** login (device OAuth), and routes tool calls through the DataTrust gateway — never directly to internal APIs.

This repository contains **only the MCP client**. It does not ship server credentials, database connection strings, or customer-specific URLs.

## Online install (needs internet)

**Requires Python 3.10+** and `pip`.

Get your manifest URL from **RightSight → Open Integration Gateway → MCP → Install MCP** (copy the setup command), then run:

```bash
pip install git+https://github.com/rightdataorg/datatrust-mcp.git
datatrust-mcp setup https://YOUR-DATATRUST-HOST/PathBase/api/MCPInstall/Config
```

Examples (replace with your host and PathBase, e.g. `/Rightdata`):

```bash
# Generic on-prem
pip install git+https://github.com/rightdataorg/datatrust-mcp.git
datatrust-mcp setup https://datatrust.yourcompany.com/Rightdata/api/MCPInstall/Config

# From a downloaded manifest file (offline manifest, online pip)
datatrust-mcp setup ./environments.json
```

What `setup` does:

1. Fetches your customer's environment manifest (dev / QA / UAT / prod URLs).
2. Writes `~/.config/datatrust-mcp/environments.json`.
3. Registers the MCP in Claude Desktop, Cursor, VS Code Copilot, Claude Code, etc.
4. Prints a report.

Restart your AI client. The **first tool call per environment** opens a browser for **your** DataTrust sign-in. Sessions are personal and cached for 30 days.

## Offline install (no internet on laptop)

Use the **Download datatrust-mcp-installer.zip** button in the product UI (served by your DataTrust deployment). Unzip and run:

```bash
python3 install.py
```

## CLI reference

```bash
datatrust-mcp setup <manifest-url-or-file>   # install / refresh
datatrust-mcp status                         # envs + signed-in user per env
datatrust-mcp envs                           # JSON for scripting
datatrust-mcp logout [--env NAME] [--all]    # revoke your session + clear cache
datatrust-mcp uninstall [--purge-tokens]     # remove from AI clients
```

Invoking `datatrust-mcp` with no args starts the stdio MCP server (what AI clients spawn).

## Tools exposed

| Tool | Purpose |
|---|---|
| `list_environments` | Show configured envs, default, sign-in status |
| `switch_default_environment` | Persist a new default env |
| `search_metadata` | Find data assets by keyword |
| `get_quality_score` | Latest DQ score |
| `get_failed_rules` | Recent failed rules |
| `workspace_summary` | Dashboard totals |
| `confirm_and_create_scenarios` | Deploy scenarios (requires scope) |
| … | See product docs for full catalog |

Every tool accepts optional `environment` (e.g. `"prod"`). Omit to use the default.

## Security model

- **Per-user auth:** each person signs in with their own DataTrust account; OAuth issues a personal `dtmcp_*` session key.
- **Per-environment isolation:** separate tokens at `~/.config/datatrust-mcp/tokens/<env>.json`.
- **No secrets in this repo:** credentials live only on the customer's DataTrust deployment and in the user's local token cache (`chmod 0600`).
- **Gateway-only:** the client talks to `{dotnet_url}/api/mcp/v1/*` and `/api/MCPAuth/*` — not FastAPI or databases directly.

## CI / Microsoft Copilot mode (service tokens)

Headless callers — CI pipelines, GitHub Actions, n8n flows, and the
**Microsoft Copilot Studio agent** that publishes DataTrust to Microsoft
Teams and M365 Copilot Chat — should NOT use the per-user device-code OAuth
flow. Instead, an admin mints a long-lived **service token**
(`dtmcp_svc_*`) from the DataTrust UI:

  RightSight → Open Integration Gateway → MCP → Clients → Service tokens → Mint token

The CLI accepts the same token on the environment variable already
documented in `.env.example`:

```bash
export DATATRUST_API_KEY=dtmcp_svc_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export DATATRUST_MCP_ALLOW_SHARED_KEY=1
datatrust-mcp setup https://datatrust.example.com/Rightdata/api/MCPInstall/Config
```

When `DATATRUST_API_KEY` is set, every call goes out with `x-api-key:
<that token>` and the device-code browser flow is skipped entirely. The
gateway recognises `dtmcp_svc_*` tokens (`TokenType='service'`) and audit-logs
the call as `Principal = svc:<token name>`, not a user email.

For the Microsoft Copilot Studio integration you do not install this CLI
at all — Copilot Studio talks to `POST {dotnet_url}/api/mcp/v1/mcp`
(Streamable HTTP) directly. See
[`DataTrust/docs/copilot/INSTALL.md`](https://github.com/rightdataorg/datatrust/blob/main/docs/copilot/INSTALL.md)
for the customer-admin runbook.

See [`SECURITY.md`](SECURITY.md) for the full threat model (includes server-side guards deployed with DataTrust).

## Customer admin (server side)

Configure once per deployment in DataTrust `appsettings.json` under `MCPInstall` (see your internal deployment guide). End users only need the manifest URL or offline ZIP from the product UI.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No DataTrust MCP configuration found` | Run `datatrust-mcp setup <url>` |
| Browser opens wrong URL / 404 on approve | Ensure manifest `dotnet_url` includes PathBase (e.g. `/Rightdata`) |
| `Could not locate datatrust-mcp executable` | Re-run `pip install git+https://github.com/rightdataorg/datatrust-mcp.git` |
| Wrong user after laptop handoff | Run `datatrust-mcp logout` then sign in again |
| Claude shows server but no tools | Restart the AI client |

## Development

```bash
pip install -e ".[dev]"
python -m datatrust_mcp status
pytest -q  # if tests added
```

## License

Copyright © RightData. See [LICENSE](LICENSE).
