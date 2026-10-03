# Original child command entrypoint

Source head 5cf5d0d8. ChildCommand owns argv and decode/run through main().
Canonical module import replaces module reexecution; the __main__ decoder is
deleted. ControllingTerminalCommand, NamespaceInitCommand and WatchDeadlineCommand
inherit the same entrypoint. No stderr filter or alternate process/codec.

Existing NRA Package parsed src/tests/tools at immutable before/after heads:
four family declarations, zero parse omissions; lexical consumers 7→6 because
the separate decoder is deleted. Generated import text was read explicitly;
AST references alone do not resolve it. Namespace/watchdog original callers retain
argv(), and Einstein owns Toad392's controlling-terminal caller.

Final source checks: real controlling PTY child same PID/session, /dev/tty open,
canonical ChildCommand module, empty stderr, exit0. Existing namespace and
inherited-deadline controls 2 passed in 2.85s. An earlier pytest invocation rejected
inherited optional-plugin arguments before collecting; its raw output stays in
.artifacts/child-command-canonical-module-controls.log.

These are real OS source checks. Einstein owns matching installed Toad392
Shell/MCP/AppPTY acceptance. No model, provider, public process or original input.
