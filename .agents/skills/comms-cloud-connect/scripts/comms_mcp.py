#!/usr/bin/env python3
"""Zero-dependency PKCE OAuth + Streamable HTTP MCP client for agent-comms.

Stores OAuth state and tokens with mode 0600 in /tmp/comms-mcp-auth.json
(override with COMMS_MCP_STATE). Never prints secrets to stdout.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "https://laptop.tail553874.ts.net/mcp"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
DEFAULT_STATE_PATH = Path(os.environ.get("COMMS_MCP_STATE", "/tmp/comms-mcp-auth.json"))
PROTOCOL_VERSION = "2025-03-26"

RULE_ORDER = ("spine", "slop", "team", "no-waiting", "evidence", "talk")
PAPER_ORDER = ("semantic-completeness", "less-is-harder")


def _write_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _origin(mcp_url: str) -> str:
    parsed = urllib.parse.urlsplit(mcp_url)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"Invalid MCP URL: {mcp_url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def _http_json(
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 65.0,
) -> tuple[int, dict[str, str], bytes]:
    req_headers = {"Accept": "application/json, text/event-stream"}
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers.items()), err.read()


def _discover_oauth(mcp_url: str) -> dict[str, str]:
    origin = _origin(mcp_url)
    status, _, raw = _http_json(f"{origin}/.well-known/oauth-authorization-server", timeout=15.0)
    if status == 200:
        meta = json.loads(raw.decode("utf-8"))
        return {
            "issuer": meta.get("issuer", origin),
            "authorization_endpoint": meta["authorization_endpoint"],
            "token_endpoint": meta["token_endpoint"],
            "registration_endpoint": meta["registration_endpoint"],
        }
    return {
        "issuer": origin,
        "authorization_endpoint": f"{origin}/authorize",
        "token_endpoint": f"{origin}/token",
        "registration_endpoint": f"{origin}/register",
    }


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def cmd_auth_start(args: argparse.Namespace) -> int:
    meta = _discover_oauth(args.url)
    status, _, raw = _http_json(
        meta["registration_endpoint"],
        method="POST",
        json_body={
            "client_name": args.client_name,
            "redirect_uris": [args.redirect_uri],
            "token_endpoint_auth_method": "none",
        },
        timeout=20.0,
    )
    if status not in (200, 201):
        sys.stderr.write(f"Client registration failed (HTTP {status}): {raw.decode('utf-8', 'replace')}\n")
        return 1
    reg = json.loads(raw.decode("utf-8"))
    client_id = reg["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = _s256(verifier)
    state_token = secrets.token_urlsafe(24)

    auth_params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": args.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state_token,
        }
    )
    authorize_url = f"{meta['authorization_endpoint']}?{auth_params}"

    existing = _read_state(args.state)
    new_state = {
        "mcp_url": args.url,
        "thread": existing.get("thread", ""),
        "oauth": meta,
        "client_id": client_id,
        "redirect_uri": args.redirect_uri,
        "pending": {
            "code_verifier": verifier,
            "state": state_token,
        },
    }
    _write_state(args.state, new_state)

    print(json.dumps({"status": "pending_approval", "mcp_url": args.url, "authorize_url": authorize_url}, indent=2))
    return 0


def _extract_code(raw_input: str, expected_state: str) -> str:
    text = raw_input.strip()
    if "://" in text or "?" in text or "code=" in text:
        parsed = urllib.parse.urlsplit(text if "://" in text else f"http://local/?{text.lstrip('?')}")
        qs = urllib.parse.parse_qs(parsed.query)
        if "error" in qs:
            raise SystemExit(f"OAuth error in callback: {qs.get('error')} {qs.get('error_description', '')}")
        if "state" in qs and expected_state and qs["state"][0] != expected_state:
            raise SystemExit("OAuth state mismatch; re-run auth-start.")
        if "code" not in qs or not qs["code"][0]:
            raise SystemExit("No 'code' parameter found in pasted URL.")
        return qs["code"][0]
    return text


def cmd_auth_finish(args: argparse.Namespace) -> int:
    st = _read_state(args.state)
    pending = st.get("pending")
    if not pending:
        sys.stderr.write("No pending OAuth request found. Run auth-start first.\n")
        return 1

    code = _extract_code(args.code_or_url, pending.get("state", ""))
    status, _, raw = _http_json(
        st["oauth"]["token_endpoint"],
        method="POST",
        form_body={
            "grant_type": "authorization_code",
            "client_id": st["client_id"],
            "code": code,
            "code_verifier": pending["code_verifier"],
            "redirect_uri": st["redirect_uri"],
        },
        timeout=20.0,
    )
    if status != 200:
        sys.stderr.write(f"Token exchange failed (HTTP {status}): {raw.decode('utf-8', 'replace')}\n")
        return 1

    tokens = json.loads(raw.decode("utf-8"))
    st.pop("pending", None)
    st["access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        st["refresh_token"] = tokens["refresh_token"]
    _write_state(args.state, st)

    # Verify with MCP initialize
    init = _mcp_rpc(args.state, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "comms-cloud-connect", "version": "1"},
    })
    print(json.dumps({
        "status": "authenticated",
        "mcp_url": st["mcp_url"],
        "thread": st.get("thread") or None,
        "serverInfo": init.get("result", {}).get("serverInfo"),
        "instructions_preview": (init.get("result", {}).get("instructions") or "").splitlines()[:3],
    }, indent=2))
    return 0


def _refresh_tokens(state_path: Path, st: dict[str, Any]) -> dict[str, Any]:
    refresh = st.get("refresh_token")
    if not refresh:
        raise SystemExit("Access token rejected (HTTP 401) and no refresh_token stored. Re-run auth-start.")
    status, _, raw = _http_json(
        st["oauth"]["token_endpoint"],
        method="POST",
        form_body={
            "grant_type": "refresh_token",
            "client_id": st["client_id"],
            "refresh_token": refresh,
        },
        timeout=20.0,
    )
    if status != 200:
        raise SystemExit(f"Token refresh failed (HTTP {status}). Re-run auth-start.")
    tokens = json.loads(raw.decode("utf-8"))
    st["access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        st["refresh_token"] = tokens["refresh_token"]
    _write_state(state_path, st)
    return st


def _decode_mcp_body(headers: dict[str, str], raw: bytes) -> dict[str, Any]:
    ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    text = raw.decode("utf-8", "replace")
    if "text/event-stream" in ctype:
        last_reply: dict[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data: "):
                event = json.loads(line[6:])
                if "result" in event or "error" in event:
                    last_reply = event
        if last_reply is None:
            raise SystemExit(f"SSE stream ended without a JSON-RPC reply: {text[:400]}")
        return last_reply
    return json.loads(text)


def _mcp_rpc(state_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    st = _read_state(state_path)
    if not st.get("access_token"):
        raise SystemExit("Not authenticated. Run auth-start and auth-finish first.")

    payload = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": method, "params": params}
    for attempt in range(2):
        status, headers, raw = _http_json(
            st["mcp_url"],
            method="POST",
            json_body=payload,
            headers={"Authorization": f"Bearer {st['access_token']}"},
            timeout=65.0,
        )
        if status == 401 and attempt == 0:
            st = _refresh_tokens(state_path, st)
            continue
        if status != 200:
            raise SystemExit(f"MCP {method} failed (HTTP {status}): {raw.decode('utf-8', 'replace')}")
        reply = _decode_mcp_body(headers, raw)
        if "error" in reply:
            raise SystemExit(f"MCP JSON-RPC error: {json.dumps(reply['error'])}")
        return reply
    raise SystemExit("MCP call failed after token refresh.")


def cmd_use_thread(args: argparse.Namespace) -> int:
    st = _read_state(args.state)
    st["thread"] = args.thread
    _write_state(args.state, st)
    print(json.dumps({"active_thread": args.thread, "mcp_url": st.get("mcp_url")}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    st = _read_state(args.state)
    if not st.get("access_token"):
        print(json.dumps({
            "authenticated": False,
            "pending": bool(st.get("pending")),
            "mcp_url": st.get("mcp_url"),
            "thread": st.get("thread") or None,
        }, indent=2))
        return 1
    init = _mcp_rpc(args.state, "initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "comms-cloud-connect", "version": "1"},
    })
    print(json.dumps({
        "authenticated": True,
        "mcp_url": st["mcp_url"],
        "thread": st.get("thread") or None,
        "serverInfo": init.get("result", {}).get("serverInfo"),
    }, indent=2))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    reply = _mcp_rpc(args.state, "tools/list", {})
    tools = reply.get("result", {}).get("tools", [])
    for t in tools:
        desc = (t.get("description") or "").splitlines()[0][:100]
        print(f"{t['name']}: {desc}")
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    try:
        tool_args = json.loads(args.arguments) if args.arguments else {}
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON arguments: {exc}") from exc
    if not isinstance(tool_args, dict):
        raise SystemExit("Tool arguments must be a JSON object.")

    if not args.no_fork and "fork" not in tool_args:
        st = _read_state(args.state)
        thread = args.thread or st.get("thread")
        if thread:
            tool_args["fork"] = {"name": thread}

    if args.tool == "comms_watch":
        raise SystemExit("Refusing to print comms_watch capability URL; use `run-watch` instead.")

    reply = _mcp_rpc(args.state, "tools/call", {"name": args.tool, "arguments": tool_args})
    result = reply.get("result", {})
    is_error = bool(result.get("isError"))
    if result.get("structuredContent"):
        print(json.dumps(result["structuredContent"], indent=2))
        extra = [c.get("text", "") for c in result.get("content", [])[1:] if c.get("text")]
        if extra:
            print("\n--- board notices ---")
            for line in extra:
                print(line)
    else:
        for item in result.get("content", []):
            text = item.get("text", "")
            try:
                print(json.dumps(json.loads(text), indent=2))
            except Exception:
                print(text)
    return 1 if is_error else 0


def _extract_watch_url(result: dict[str, Any]) -> str | None:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and isinstance(structured.get("url"), str):
        return structured["url"]
    for item in result.get("content", []):
        text = item.get("text", "")
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("url"), str):
            return parsed["url"]
    return None


def cmd_run_watch(args: argparse.Namespace) -> int:
    tool_args: dict[str, Any] = {}
    if not args.no_fork:
        st = _read_state(args.state)
        thread = args.thread or st.get("thread")
        if thread:
            tool_args["fork"] = {"name": thread}

    reply = _mcp_rpc(args.state, "tools/call", {"name": "comms_watch", "arguments": tool_args})
    result = reply.get("result", {})
    if result.get("isError"):
        for item in result.get("content", []):
            sys.stderr.write(f"{item.get('text', '')}\n")
        return 1
    url = _extract_watch_url(result)
    if not url:
        raise SystemExit("comms_watch did not return a watch URL.")

    extra_args: list[str] = []
    if args.transcript:
        extra_args.extend(["--transcript", str(args.transcript)])
    if args.once:
        extra_args.append("--once")

    watch_bin = shutil.which("agent-comms-inbox-watch")
    if watch_bin:
        return subprocess.run([watch_bin, url, *extra_args]).returncode

    root = _find_agent_comms_root(args.repo)
    uv_bin = shutil.which("uv")
    if root is not None and uv_bin:
        return subprocess.run(
            [uv_bin, "run", "--locked", "--no-dev", "agent-comms-inbox-watch", url, *extra_args],
            cwd=str(root),
        ).returncode

    raise SystemExit(
        "agent-comms-inbox-watch not found on PATH and no agent-comms checkout + uv found.\n"
        "Install agent-comms or pass --repo /path/to/agent-comms."
    )


def cmd_wake_on(args: argparse.Namespace) -> int:
    log = Path(args.log).expanduser()
    while not log.exists():
        time.sleep(0.5)
    pos = args.offset if args.offset is not None else log.stat().st_size
    while True:
        time.sleep(args.poll)
        cur = log.stat().st_size
        if cur > pos:
            with log.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read().decode("utf-8", errors="replace")
            sys.stdout.write(chunk)
            sys.stdout.flush()
            return 0
        if cur < pos:
            pos = cur


def _find_agent_comms_root(hint: str | None) -> Path | None:
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint).expanduser())
    cwd = Path.cwd()
    candidates.extend([
        cwd,
        cwd.parent / "agent-comms",
        Path("/tmp/agent-comms-rules"),
        Path.home() / "code" / "projects" / "agent-comms",
    ])
    for cand in candidates:
        if (cand / "src" / "agent_comms" / "team" / "rules").is_dir():
            return cand.resolve()
    return None


def cmd_list_deal(args: argparse.Namespace) -> int:
    root = _find_agent_comms_root(args.repo)
    if root is None:
        sys.stderr.write(
            "agent-comms checkout not found in workspace.\n"
            "Clone shallowly or read from OpenHCSDev/agent-comms:\n"
            "  git clone --depth 1 https://github.com/OpenHCSDev/agent-comms.git /tmp/agent-comms-rules\n"
            "  python3 comms_mcp.py list-deal /tmp/agent-comms-rules\n"
        )
        return 1

    team = root / "src" / "agent_comms" / "team"
    files: list[Path] = []
    if args.variant in ("full", "no-patterns"):
        for paper_dir in PAPER_ORDER:
            pdir = team / "paper" / paper_dir
            if pdir.is_dir():
                files.extend(sorted(pdir.glob("*.md")))

    rules_dir = team / "rules"
    rule_files = sorted(
        (p for p in rules_dir.iterdir() if p.is_file()),
        key=lambda p: (RULE_ORDER.index(p.stem) if p.stem in RULE_ORDER else len(RULE_ORDER), p.name),
    )
    files.extend(rule_files)

    if args.variant != "light":
        for extra in (
            team / "voice.md",
            team / "pattern_sources.py",
            root / "src" / "agent_comms" / "skills" / "comms-team" / "SKILL.md",
        ):
            if extra.is_file():
                files.append(extra)

    if args.variant in ("full", "no-papers"):
        nra_candidates = [
            root.parent / "NominalRefactorAdvisor" / "src" / "nominal_refactor_advisor" / "audit" / "patterns",
            Path.home() / "code" / "projects" / "NominalRefactorAdvisor" / "src" / "nominal_refactor_advisor" / "audit" / "patterns",
        ]
        for nra in nra_candidates:
            if (nra / "README.md").is_file():
                files.append(nra / "README.md")
                files.extend(sorted(p for p in nra.glob("*.md") if p.name != "README.md"))
                break
        if (team / "patterns.md").is_file():
            files.append(team / "patterns.md")

    for path in files:
        print(path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PKCE OAuth + MCP helper for agent-comms cloud sessions.")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="Token/state JSON path (mode 0600)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("auth-start", help="Register PKCE client and print approval URL")
    p_start.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint URL (default: {DEFAULT_URL})")
    p_start.add_argument("--client-name", default="antigravity-cloud", help="OAuth client name shown on approval")
    p_start.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI, help="Loopback redirect URI")
    p_start.set_defaults(func=cmd_auth_start)

    p_fin = sub.add_parser("auth-finish", help="Exchange pasted redirect URL or ?code= for tokens")
    p_fin.add_argument("code_or_url", help="Full callback URL or bare authorization code")
    p_fin.set_defaults(func=cmd_auth_finish)

    p_use = sub.add_parser("use-thread", help="Set default bus thread name for subsequent tool calls")
    p_use.add_argument("thread", help="Bus thread name (passed as fork={name: thread})")
    p_use.set_defaults(func=cmd_use_thread)

    p_stat = sub.add_parser("status", help="Check auth status and run MCP initialize")
    p_stat.set_defaults(func=cmd_status)

    p_tools = sub.add_parser("tools", help="List available MCP tools")
    p_tools.set_defaults(func=cmd_tools)

    p_call = sub.add_parser("call", help="Invoke an MCP tool by name")
    p_call.add_argument("tool", help="MCP tool name (e.g. comms_threads, comms_send, comms_thread_tags)")
    p_call.add_argument("arguments", nargs="?", default="{}", help="JSON object of tool arguments")
    p_call.add_argument("--thread", default="", help="Override fork thread name for this call")
    p_call.add_argument("--no-fork", action="store_true", help="Do not inject fork={name: ...}")
    p_call.set_defaults(func=cmd_call)

    p_watch = sub.add_parser("run-watch", help="Open a persistent inbox watch via agent-comms-inbox-watch")
    p_watch.add_argument("--thread", default="", help="Override fork thread name for this watch")
    p_watch.add_argument("--no-fork", action="store_true", help="Watch the top-level connector thread without fork")
    p_watch.add_argument("--repo", default=None, help="Path to agent-comms checkout when agent-comms-inbox-watch is not on PATH")
    p_watch.add_argument("--transcript", default=None, help="Explicit Antigravity transcript.jsonl path")
    p_watch.add_argument("--once", action="store_true", help="Exit 0 after printing one delivered message (for harnesses waking on exit)")
    p_watch.set_defaults(func=cmd_run_watch)

    p_wake = sub.add_parser("wake-on", help="Exit with new lines as soon as a run-watch log grows (reactive wakeup)")
    p_wake.add_argument("log", help="Path to the run-watch daemon log file")
    p_wake.add_argument("--poll", type=float, default=2.0, help="Poll interval in seconds (default: 2.0)")
    p_wake.add_argument("--offset", type=int, default=None, help="Starting byte offset (default: current file size)")
    p_wake.set_defaults(func=cmd_wake_on)

    p_deal = sub.add_parser("list-deal", help="Print ordered paper/rule/pattern files to read in full")
    p_deal.add_argument("repo", nargs="?", default=None, help="Path to agent-comms checkout")
    p_deal.add_argument(
        "--variant",
        choices=("full", "no-papers", "no-patterns", "light"),
        default="full",
        help="Deal variant matching agent-comms-prime",
    )
    p_deal.set_defaults(func=cmd_list_deal)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
