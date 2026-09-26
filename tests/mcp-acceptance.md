# Provider-free MCP linked acceptance

`test_mcp_acceptance.py` is opt-in. It runs an explicitly supplied native-input-ID
Pi build, the actual MCP package and SDK stdio server, ACP, and a real detached
Unix-socket attachment. It never installs packages or uses a paid provider.

```sh
env -i HOME="$HOME" PATH="$PATH" \
  PYTHONPATH="$PWD/src:/absolute/to/toad/src" \
  TMPDIR=/absolute/to/home-test-tmp \
  AC_MCP_NATIVE_BIN=/absolute/to/prepared/pi-native \
  AC_MCP_TOAD_ADAPTER=/absolute/to/toad/tests/mcp_linked_adapter.py \
  /absolute/to/compatible/python -m pytest -q -o addopts='' \
  tests/test_mcp_acceptance.py --basetemp=/absolute/to/fresh-evidence-directory
```

Use an already prepared, verified native Pi launcher, not stock Pi. The backend
must independently attest its native-input capability before submitting any
prompt. No tracked/uncertain input is resumed: HOME, Pi agent directory, project,
wire, sessions and input identities are disposable. The supplied launcher is
read-only; `launch.json` records its path and hash. Verify its implementation
manifest separately and retain that log alongside the evidence.

A loopback-only OpenAI-compatible SSE fixture supplies exactly two model
responses. Pi itself dispatches the discovered MCP tool and handles its result.
A Node fetch guard refuses any origin other than this one loopback endpoint;
the child receives a minimal environment and only a fake API key. The first
response waits for the native package receipt to reach ACP. The second response
is held during the mid-turn retirement check. The fixture allows no retries.
No slash-command substitute or fake Pi event emitter is used.

Cases:

- `allow`: explicit simulated-user Allow once produces exactly one MCP echo.
- `no_controller`: a passive attachment observes status but receives no dialog;
  the real tool dispatch is denied and never reaches the MCP server.
- `revoke_midturn`: while Pi awaits the actual permission response, a separate
  acceptance-owned PTY displays the declaration and types the exact package CLI
  denial challenge. Returning Allow once cannot override the changed ledger.
  The final mock response remains held while the active ACP turn and absent MCP
  PID are checked, proving retirement before settlement or Pi shutdown.
- `disconnect`: close the actual controlling Unix socket during its pending
  permission. No MCP call occurs; a passive audit observer records settlement.

Every case asserts one receipt within the owning ACP started/settled interval,
matching turn ID and session. All MCP child PIDs must be absent after cleanup.
PTY reads and output are bounded; descriptors and CLI processes are closed in
`finally`. Saved files include ACP updates, local model requests/errors, launch
identity, case evidence, and the simulated-user PTY/midturn receipt where used.

## Actual Toad adapter contract

Without `AC_MCP_TOAD_ADAPTER`, this suite proves **only Pi/package/ACP**; it must
not be cited as actual Toad rendering evidence.

The Toad owner supplies a module with an async context manager:

```python
async with open_observer(case: str, artifact_dir: Path) as observer:
    await observer.session_update(session_id=..., update=...)
    await observer.request_permission(session_id=..., tool_call=..., options=...)
    await observer.disconnected()  # disconnect case only
```

`update`, `tool_call` and options are raw ACP JSON dictionaries from the actual
RuntimeProxy attachment, not pydantic models. `request_permission` returns an ACP
permission response with the standard `outcome` object, using explicit simulated
UI input only. The harness withholds that response until the appropriate test
barrier is released. For the disconnect case, the observer must expose an
`asyncio.Event` named `permission_presented`, set only after the actual Ask is
mounted (and its permission screenshot captured). The harness waits for it
before closing the socket, proving retirement of a visible pending dialog,
not merely cancellation of a queued RPC. The observer should exercise actual Agent/Conversation
rendering and retain UI evidence. It must check stale-first-successor-turn,
foreign-session, and settled receipts fail closed, and disconnect clears the
live view. Non-disconnect cases drain the attachment's settlement update before
adapter exit. Toad owns its adapter and UI assertions; this repository does not
copy Toad code, MCP configuration, ledger authority, or CLI challenge automation
into production UI.
