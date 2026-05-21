# Security — datatrust-mcp (client)

This repository ships the **local MCP client** only. Server-side guardrails (SQL injection prevention, rate limits, audit logging, scope enforcement) are deployed with **DataTrust** and **ObservabilityPython** on the customer’s infrastructure — not in this repo.

## What this client stores locally

| Path | Contents | Permissions |
|---|---|---|
| `~/.config/datatrust-mcp/environments.json` | Customer manifest (public URLs per env) | user read/write |
| `~/.config/datatrust-mcp/tokens/<env>.json` | OAuth session token + metadata | **0600** |

Tokens are personal. Each environment has its own file. Deleting one does not affect others.

## Authentication model

1. **Default (recommended):** Device OAuth (RFC 8628) via the customer’s DataTrust `.NET` app (`/api/MCPAuth/Device/*`). The user signs in with their own account; the gateway mints a per-user `dtmcp_*` session key.
2. **CI / automation only:** `DATATRUST_API_KEY` bypasses OAuth when explicitly allowed via `DATATRUST_MCP_ALLOW_SHARED_KEY=1`. The CLI warns if a shared key is detected without that flag.

Never commit `.env`, token files, or API keys to git.

## Network boundaries

The client only calls:

- `{dotnet_url}/api/MCPInstall/Config` — manifest (during `setup`)
- `{dotnet_url}/api/MCPAuth/*` — OAuth
- `{dotnet_url}/api/mcp/v1/*` — tool proxy (gateway forwards to internal FastAPI)

It does **not** connect to databases, Supabase, or FastAPI directly in the normal install path.

## Reporting vulnerabilities

Email **security@getrightdata.com** with reproduction steps. Do not open public GitHub issues for security reports.

## Server-side reference (not in this repo)

Customer operators configure:

- `DATATRUST_MCP_INTERNAL_SECRET` — shared secret between .NET gateway and FastAPI
- `MCPAUTH_MSSQL_CONNECTION` — SQL Server for OAuth tables (env var only, never in git)
- `MCPInstall:AllowPublic` — whether unauthenticated manifest fetch is allowed (default: false)

See your internal DataTrust deployment guide for the full server threat model.
