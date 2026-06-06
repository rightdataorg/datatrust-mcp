"""Browser-based OAuth handshake against the DataTrust .NET app.

Flow:
  1. Start a loopback HTTP listener on a random port.
  2. Open the user's browser to
       <DataTrust>/api/MCPAuth/Authorize?state=...&redirect_uri=http://127.0.0.1:<port>/cb
     .NET will redirect them to /Account/Login if needed, then to our
     redirect URI with `?code=...&state=...`.
  3. Receive the callback locally, verify state, exchange the code for a
     30-day MCP session token at <DataTrust>/api/MCPAuth/Token.
  4. Persist the token to ~/.config/datatrust-mcp/token (mode 0600) so we
     never have to do this dance again until it expires.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

TOKEN_DIR = Path(os.path.expanduser("~/.config/datatrust-mcp"))
# Legacy single-token path — kept readable so users who upgrade don't have
# to re-OAuth. Per-env tokens live at TOKEN_DIR/tokens/<env_name>.json now.
LEGACY_TOKEN_PATH = TOKEN_DIR / "token"


def verify_tls() -> bool:
    """Whether httpx should verify TLS certificates for DataTrust calls.

    Defaults to False to (a) stay consistent with the install/setup path
    (cli.py uses verify=False) and (b) work with internal DataTrust hosts
    that present self-signed or incomplete-chain certs — Python's certifi
    bundle rejects those even when the OS keychain accepts them. Set
    DATATRUST_MCP_VERIFY_TLS=1 (or true/yes/on) to enforce strict
    verification on deployments with fully-valid public certs.
    """
    v = (os.environ.get("DATATRUST_MCP_VERIFY_TLS") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _token_path_for(env_name: str | None) -> Path:
    """Return the on-disk path for a given environment's cached token.
    If env_name is None / 'default', honor the legacy path so existing
    single-env users don't lose their session."""
    if env_name is None or env_name == "default":
        return LEGACY_TOKEN_PATH
    (TOKEN_DIR / "tokens").mkdir(parents=True, exist_ok=True)
    return TOKEN_DIR / "tokens" / f"{env_name}.json"


def load_token(env_name: str | None = None) -> dict | None:
    """Return persisted token dict for the named env, or None."""
    p = _token_path_for(env_name)
    try:
        raw = p.read_text()
        d = json.loads(raw)
        if not isinstance(d, dict) or "access_token" not in d:
            return None
        return d
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def save_token(token_data: dict, env_name: str | None = None) -> None:
    p = _token_path_for(env_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    if env_name:
        token_data.setdefault("env_name", env_name)
    p.write_text(json.dumps(token_data, indent=2))
    try:
        p.chmod(0o600)
    except Exception:
        pass


def clear_token(env_name: str | None = None) -> None:
    try:
        _token_path_for(env_name).unlink()
    except FileNotFoundError:
        pass


# Back-compat alias for older callers that reference TOKEN_PATH directly.
TOKEN_PATH = LEGACY_TOKEN_PATH


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures ?code= and shuts the server down."""

    server_state: dict  # set on the server before serve_forever

    def log_message(self, *_):  # silence stdout
        pass

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        err = (params.get("error") or [None])[0]

        st = self.server.server_state  # type: ignore[attr-defined]
        if err:
            st["error"] = err
            body = f"<h1>Error</h1><pre>{err}</pre>"
        elif state != st.get("expected_state"):
            st["error"] = "state mismatch"
            body = "<h1>Error</h1><p>State mismatch. Try again.</p>"
        elif not code:
            st["error"] = "no code in callback"
            body = "<h1>Error</h1><p>No code received.</p>"
        else:
            st["code"] = code
            # Bounce the browser to the .NET success page so the user sees a
            # branded "you can close this tab" message instead of localhost.
            success_url = st.get("success_url") or "about:blank"
            self.send_response(302)
            self.send_header("Location", success_url)
            self.end_headers()
            st["done"].set()
            return

        self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())
        st["done"].set()


def run_oauth_flow(
    datatrust_url: str,
    *,
    env_name: str | None = None,
    env_label: str | None = None,
    timeout_seconds: int = 300,
) -> dict:
    """Block until the user signs in. Returns token dict; raises on failure.

    `datatrust_url` is the .NET base URL (e.g. http://localhost:5000).
    `env_name` selects which on-disk token slot to write to (per-env cache).
    `env_label` is shown to the user in stderr ("Sign in to <prod>...").
    """
    base = datatrust_url.rstrip("/")
    state = secrets.token_urlsafe(24)
    port = _free_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}/cb"

    server_state: dict = {
        "expected_state": state,
        "code": None,
        "error": None,
        "done": threading.Event(),
        "success_url": f"{base}/api/MCPAuth/Success",
    }

    server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    server.server_state = server_state  # type: ignore[attr-defined]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    auth_url = (
        f"{base}/api/MCPAuth/Authorize"
        f"?state={urllib.parse.quote(state)}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri)}"
    )

    # stderr so Claude Desktop's MCP log captures it; stdout is the JSON-RPC
    # channel and must stay clean.
    label = f" ({env_label})" if env_label else ""
    print(
        f"[datatrust-mcp] Opening browser for DataTrust sign-in{label}: {auth_url}",
        file=sys.stderr, flush=True,
    )
    try:
        webbrowser.open(auth_url, new=2, autoraise=True)
    except Exception as exc:
        print(f"[datatrust-mcp] Could not auto-open browser: {exc}", file=sys.stderr)

    finished = server_state["done"].wait(timeout=timeout_seconds)
    server.shutdown()

    if not finished:
        # Before giving up, check if the user completed the OAuth manually
        # via `datatrust-mcp activate <env> <code>` while we were waiting.
        # If a token landed on disk for this env, treat it as success.
        if env_name:
            persisted = load_token(env_name)
            if persisted and persisted.get("access_token"):
                print(
                    f"[datatrust-mcp] Picked up a token written by "
                    f"`datatrust-mcp activate` while waiting.",
                    file=sys.stderr, flush=True,
                )
                return persisted
        raise RuntimeError(
            "DataTrust sign-in didn't complete within the timeout. "
            "If the browser is stuck or your network blocks the loopback "
            "callback, copy the code from the DataTrust Success page and run:\n"
            f"    datatrust-mcp activate <env-name> <code>\n"
            f"Then retry your MCP request. Sign-in URL was:\n    {auth_url}"
        )
    if server_state["error"]:
        raise RuntimeError(f"DataTrust sign-in failed: {server_state['error']}")
    if not server_state["code"]:
        raise RuntimeError("DataTrust sign-in finished without a code.")

    # Exchange the code for a session token
    with httpx.Client(timeout=30.0, verify=verify_tls()) as client:
        resp = client.post(
            f"{base}/api/MCPAuth/Token",
            json={"code": server_state["code"]},
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Code exchange failed ({resp.status_code}): {resp.text[:300]}"
        )

    token = resp.json()
    token["obtained_at"] = int(time.time())
    token["env_name"] = env_name
    save_token(token, env_name=env_name)
    raw = token.get("access_token") or ""
    prefix = raw[:12] + "…" if raw else "<empty>"
    saved_at = _token_path_for(env_name)
    print(
        f"[datatrust-mcp] Signed in to {env_label or 'DataTrust'} "
        f"as {token.get('user_email')} "
        f"(token prefix {prefix}). Saved to {saved_at}",
        file=sys.stderr, flush=True,
    )
    return token


# ============================================================================
# Device Authorization Grant (RFC 8628)
# ============================================================================
#
# The loopback flow above breaks on HTTPS-deployed DataTrust hosts (mixed-
# content / HSTS blocks the cross-protocol redirect from
# https://datatrust.example.com → http://127.0.0.1:NNNN).
#
# Device flow has no redirect at all:
#   1. POST /api/MCPAuth/Device/Init  → server returns device_code + user_code
#      + verification_uri.
#   2. We print the URL + short code to stderr and try to open the browser.
#   3. User signs in (if needed), enters the code, clicks Approve.
#   4. We poll POST /api/MCPAuth/Device/Token every `interval` seconds; once
#      the row flips to "approved" the server returns the same token shape
#      as the loopback /Token endpoint.
#
# RFC 8628 errors we honour during polling:
#   authorization_pending → keep polling at current interval
#   slow_down             → bump interval by 5 seconds (defensive)
#   expired_token         → give up; surface a "code expired" error
#   access_denied         → user clicked Deny; give up
#   invalid_device_code   → server lost the row; give up


class GatewayNotDeployedError(RuntimeError):
    """Raised when the DataTrust host responds, but doesn't have the MCP
    controllers in its route table yet (typical sign: the AllowAnonymous
    Device/Init endpoint 302s to /Identity/Account/Login). This is a deploy
    gap — not a user-fixable error — so we surface it distinctly."""


def _interpret_init_response(resp: "httpx.Response", url: str) -> dict:
    """Return the parsed init payload, or raise a useful error explaining
    exactly what came back over the wire. Covers three failure modes that
    have actually bitten us in the field:

      • 3xx → cookie auth middleware kicked in. Either AllowAnonymous is
        missing on Device/Init, or the deployed binary predates the MCP
        controllers. We surface the Location header so the user sees the
        redirect target (usually /Identity/Account/Login).
      • Non-JSON 2xx → the route matched an MVC view that renders HTML
        (e.g. a catch-all). Same root cause: wrong build deployed.
      • 4xx/5xx → expose the server's body verbatim (truncated)."""
    if 300 <= resp.status_code < 400:
        location = resp.headers.get("location", "<no Location header>")
        raise GatewayNotDeployedError(
            f"POST {url} returned HTTP {resp.status_code} → {location}\n"
            f"This means the DataTrust host doesn't have the MCP auth "
            f"controllers in its current build. Ask whoever runs the host "
            f"to deploy a DataTrust build that includes MCPAuthController "
            f"(commit a7477cd03 or newer on develop_dt_rs)."
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Device/Init failed ({resp.status_code}): {resp.text[:300]}"
        )
    ctype = resp.headers.get("content-type", "")
    if "json" not in ctype.lower():
        snippet = resp.text[:200].replace("\n", " ")
        raise GatewayNotDeployedError(
            f"POST {url} returned HTTP 200 but content-type was {ctype!r} "
            f"instead of JSON. The route matched an HTML handler — the MCP "
            f"controllers are not in this deployed build.\n"
            f"First 200 chars of body: {snippet}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Device/Init returned malformed JSON: {exc}. "
            f"First 200 chars: {resp.text[:200]!r}"
        ) from exc


def run_device_flow(
    datatrust_url: str,
    *,
    env_name: str | None = None,
    env_label: str | None = None,
    client_name: str | None = None,
    timeout_seconds: int = 600,
) -> dict:
    """Sign in via the device-code flow. Returns persisted token dict."""
    base = datatrust_url.rstrip("/")

    # 1. Kick off the device session.
    # follow_redirects=False so we can DETECT the cookie-auth challenge
    # instead of silently following it into a login HTML page and then
    # blowing up on .json().
    init_url = f"{base}/api/MCPAuth/Device/Init"
    with httpx.Client(timeout=30.0, follow_redirects=False, verify=verify_tls()) as client:
        init_resp = client.post(
            init_url,
            json={"client_name": client_name or "datatrust-mcp"},
        )
    init = _interpret_init_response(init_resp, init_url)
    device_code = init["device_code"]
    user_code = init["user_code"]
    verification_uri = init["verification_uri"]
    verification_uri_complete = init.get("verification_uri_complete") or verification_uri
    interval = int(init.get("interval") or 5)
    server_expires_in = int(init.get("expires_in") or 600)

    # 2. Tell the user where to go.
    label = f" ({env_label})" if env_label else ""
    msg = (
        f"\n[datatrust-mcp] Sign in to {env_label or 'DataTrust'}{label}:\n"
        f"    1. Open: {verification_uri}\n"
        f"    2. Enter code: {user_code}\n"
        f"    (Or click the pre-filled URL: {verification_uri_complete})\n"
        f"\nWaiting up to {min(timeout_seconds, server_expires_in)}s for you to approve…\n"
    )
    print(msg, file=sys.stderr, flush=True)
    try:
        webbrowser.open(verification_uri_complete, new=2, autoraise=True)
    except Exception as exc:
        print(f"[datatrust-mcp] Could not auto-open browser: {exc}", file=sys.stderr)

    # 3. Poll /Device/Token until approved/denied/expired.
    deadline = time.time() + min(timeout_seconds, server_expires_in)
    cur_interval = max(1, interval)
    last_progress = time.time()
    token_url = f"{base}/api/MCPAuth/Device/Token"
    while time.time() < deadline:
        time.sleep(cur_interval)
        try:
            with httpx.Client(timeout=15.0, follow_redirects=False, verify=verify_tls()) as client:
                resp = client.post(
                    token_url,
                    json={"device_code": device_code},
                )
        except httpx.HTTPError as exc:
            # Transient network blip — keep trying until the deadline.
            print(f"[datatrust-mcp] Poll error (will retry): {exc}",
                  file=sys.stderr, flush=True)
            continue

        # Cookie-auth challenge means the deploy regressed mid-flight.
        # Don't spin silently — surface it so the user knows what changed.
        if 300 <= resp.status_code < 400:
            location = resp.headers.get("location", "<no Location header>")
            raise GatewayNotDeployedError(
                f"POST {token_url} returned HTTP {resp.status_code} → "
                f"{location}. The MCPAuth controller disappeared from the "
                f"deployed build between Device/Init and Device/Token — "
                f"someone redeployed an older binary. Re-run after the "
                f"correct build is back."
            )

        if resp.status_code == 200:
            token = resp.json()
            token["obtained_at"] = int(time.time())
            token["env_name"] = env_name
            save_token(token, env_name=env_name)
            raw = token.get("access_token") or ""
            prefix = raw[:12] + "…" if raw else "<empty>"
            saved_at = _token_path_for(env_name)
            print(
                f"[datatrust-mcp] Signed in to {env_label or 'DataTrust'} "
                f"as {token.get('user_email')} "
                f"(token prefix {prefix}). Saved to {saved_at}",
                file=sys.stderr, flush=True,
            )
            return token

        # 4xx → look at the RFC-8628 error code in the body.
        err = None
        try:
            err = (resp.json() or {}).get("error")
        except Exception:
            err = None

        if err == "authorization_pending":
            # Print a heartbeat every ~30s so the user knows we're still alive.
            if time.time() - last_progress > 30:
                remaining = int(deadline - time.time())
                print(
                    f"[datatrust-mcp] Still waiting for approval… "
                    f"({remaining}s left, code: {user_code})",
                    file=sys.stderr, flush=True,
                )
                last_progress = time.time()
            continue
        if err == "slow_down":
            cur_interval += 5
            continue
        if err == "expired_token":
            raise RuntimeError(
                "Device code expired before you approved it. Re-run your "
                "MCP request to get a fresh code."
            )
        if err == "access_denied":
            raise RuntimeError(
                "You denied the device approval. Re-run to try again."
            )
        if err == "invalid_device_code":
            raise RuntimeError(
                "Server lost track of this device code (was the DataTrust "
                "app restarted?). Re-run to retry."
            )
        # Unknown shape — surface it verbatim and bail.
        raise RuntimeError(
            f"Device/Token failed ({resp.status_code}): {resp.text[:300]}"
        )

    raise RuntimeError(
        f"Device sign-in timed out after {timeout_seconds}s. The code "
        f"{user_code} was never approved at {verification_uri}."
    )
