"""Environment registry for the DataTrust MCP server.

The MCP can talk to many DataTrust deployments at once — typically the
customer's dev / qa / prod / demo. Configuration lives at:

    ~/.config/datatrust-mcp/environments.json

Sample shape:

    {
      "version": 1,
      "customer": "acme-corp",
      "default_environment": "dev",
      "environments": {
        "dev":  {"label": "Development",  "fastapi_url": "http://dev-datatrust.acme.local:8000",
                  "dotnet_url":  "http://dev-datatrust.acme.local:5000"},
        "qa":   {"label": "QA",           "fastapi_url": "https://qa-datatrust.acme.local",
                  "dotnet_url":  "https://qa-datatrust.acme.local"},
        "prod": {"label": "Production",   "fastapi_url": "https://datatrust.acme.local",
                  "dotnet_url":  "https://datatrust.acme.local"},
        "demo": {"label": "Demo",         "fastapi_url": "https://demo-datatrust.acme.local",
                  "dotnet_url":  "https://demo-datatrust.acme.local"}
      }
    }

Backward compatibility:
    If environments.json is missing, we fall back to a single synthetic
    env named "default" built from the legacy env vars
    DATATRUST_FASTAPI_URL / DATATRUST_DOTNET_URL. This keeps the existing
    single-customer setup working.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CONFIG_DIR = Path(os.path.expanduser("~/.config/datatrust-mcp"))
ENV_CONFIG_PATH = CONFIG_DIR / "environments.json"
TOKENS_DIR = CONFIG_DIR / "tokens"
STATE_PATH = CONFIG_DIR / "state.json"   # remembers the user's chosen default-override


@dataclass(frozen=True)
class Environment:
    """One DataTrust environment the MCP can route to.

    `dotnet_url` is the single endpoint the stdio MCP needs — it's the
    .NET gateway that fronts auth + audit + the tool-execution proxy.
    `fastapi_url` is kept as an optional, defaulted field strictly for
    backward compatibility with v1.0/v1.1 manifests that still include
    it; the runtime never reads it and v1.2+ manifests omit it.
    """

    name: str
    label: str
    dotnet_url: str
    fastapi_url: str = ""   # deprecated; default empty so v1.2+ manifests load cleanly

    def token_path(self) -> Path:
        TOKENS_DIR.mkdir(parents=True, exist_ok=True)
        return TOKENS_DIR / f"{self.name}.json"


@dataclass(frozen=True)
class Registry:
    customer: str | None
    default: str
    environments: dict[str, Environment]
    source: str   # "file" | "fallback-env-vars"

    def get(self, name: str | None) -> Environment:
        """Resolve a name (None = default) to an Environment.
        Raises KeyError with a helpful message if the name is unknown."""
        name = (name or self.default).strip()
        env = self.environments.get(name)
        if env is None:
            known = ", ".join(sorted(self.environments.keys())) or "(none)"
            raise KeyError(
                f"Unknown environment {name!r}. Configured environments: {known}. "
                f"Run `datatrust-mcp setup <url-or-file>` to install / refresh."
            )
        return env

    def names(self) -> list[str]:
        return sorted(self.environments.keys())


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def _from_legacy_env_vars() -> Registry | None:
    """Backward-compat: synthesize a one-env registry from legacy env vars.

    Only DATATRUST_DOTNET_URL is required in the new architecture; the
    stdio MCP no longer needs to know FastAPI's URL.
    """
    dotnet = os.environ.get("DATATRUST_DOTNET_URL")
    if not dotnet:
        return None
    fastapi = (os.environ.get("DATATRUST_FASTAPI_URL") or "").rstrip("/")
    env = Environment(name="default", label="default",
                      fastapi_url=fastapi, dotnet_url=dotnet.rstrip("/"))
    return Registry(customer=None, default="default",
                    environments={"default": env}, source="fallback-env-vars")


def load_registry() -> Registry:
    """Load environments.json. Falls back to legacy env vars. Honors the
    user's persisted default-override at state.json."""
    state = _load_state()

    if ENV_CONFIG_PATH.exists():
        raw = json.loads(ENV_CONFIG_PATH.read_text())
        envs: dict[str, Environment] = {}
        for name, body in (raw.get("environments") or {}).items():
            if not isinstance(body, dict):
                continue
            dnet = (body.get("dotnet_url") or "").rstrip("/")
            if not dnet:
                continue  # dotnet_url is the only required field now
            # fastapi_url is optional in v1.1+ manifests since the
            # stdio MCP never calls FastAPI directly. Default to "" so
            # the dataclass stays satisfied.
            fapi = (body.get("fastapi_url") or "").rstrip("/")
            envs[name] = Environment(
                name=name,
                label=body.get("label") or name,
                fastapi_url=fapi,
                dotnet_url=dnet,
            )
        if not envs:
            # Empty config? Fall back to env vars rather than blow up at startup.
            legacy = _from_legacy_env_vars()
            if legacy:
                return legacy
            raise RuntimeError(
                f"{ENV_CONFIG_PATH} has no environments. Run `datatrust-mcp setup`."
            )
        default = state.get("default_environment") \
                  or raw.get("default_environment") \
                  or next(iter(envs.keys()))
        if default not in envs:
            default = next(iter(envs.keys()))
        return Registry(
            customer=raw.get("customer"),
            default=default,
            environments=envs,
            source="file",
        )

    legacy = _from_legacy_env_vars()
    if legacy:
        return legacy

    raise RuntimeError(
        "No DataTrust MCP configuration found. Run:\n"
        "    datatrust-mcp setup https://<your-datatrust>/api/MCPInstall/Config\n"
        "or set DATATRUST_FASTAPI_URL / DATATRUST_DOTNET_URL env vars for a "
        "single-env legacy setup."
    )


# ---------------------------------------------------------------------------
# Mutate
# ---------------------------------------------------------------------------

def set_default_environment(name: str) -> Registry:
    """Persist the user's chosen default. Used by the switch_default_environment tool."""
    reg = load_registry()
    if name not in reg.environments:
        known = ", ".join(reg.names())
        raise KeyError(f"Unknown environment {name!r}. Known: {known}")
    state = _load_state()
    state["default_environment"] = name
    _save_state(state)
    # Return a fresh registry that reflects the new default
    return Registry(customer=reg.customer, default=name,
                    environments=reg.environments, source=reg.source)


def write_environments_file(payload: dict) -> Path:
    """Write a new environments.json (used by the `setup` CLI)."""
    if not isinstance(payload, dict) or "environments" not in payload:
        raise ValueError("payload must contain an 'environments' object")
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENV_CONFIG_PATH.write_text(json.dumps(payload, indent=2))
    try:
        ENV_CONFIG_PATH.chmod(0o600)
    except Exception:
        pass
    return ENV_CONFIG_PATH
