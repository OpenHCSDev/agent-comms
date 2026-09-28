# Installed native tools on restricted service PATH

The first post209 copied-session test manually prepended runtime/bin. That missed
actual workers' PATH=/usr/local/bin:/usr/bin. Their global agent-comms extension
failed spawnSync agent-comms ENOENT. The first live harness also falsely matched
the echoed user prompt; its receipt is explicitly invalidated and retained.

The native launcher now prepends its own Python installation's bin directory
before execing Pi. Global extension child CLIs use the same installed Comms.
No extension, source validation or saved data guard is disabled.

Actual acceptance, not mocked backends:
- Installed native wrapper + exact existing worker environment + copied143MB
  saved session: normal automatic extensions, native get_state succeeds.
- Existing live owner via ACP: assistant-only matching returns ACP_STARTUP_FIX_OK.
- Installed `python -m agent_comms.acp` over stdio, same invocation as Toad:
  fresh ACP_STDIO_PATH_OK assistant reply, corroborated by a NEW native assistant
  message with successful stop,10.6s. Same owner PID. No failed-message replay.
- Owner confirmed real Toad sending: "working now".

The first two tests have narrower scope than the third. The stdio receipt plus
owner confirmation closes the actual reported send defect. CI deferred.
