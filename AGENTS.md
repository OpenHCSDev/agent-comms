# Working on agent-comms and the paired Toad stack

Read the current project prompt at `.pi/APPEND_SYSTEM.md` when available and the
standing owner decisions in `docs/DECISIONS.md`. Follow the latest NRA and
refactor-audit skills. Current owner instructions supersede old plan holds.

- Work in persistent isolated worktrees under `/home/ts/wt`. Preserve the dirty
  main checkout, other agents' work, native sessions and uncertain input records.
- Fix a demonstrated defect yourself or assign a named implementation owner at
  the next safe checkpoint. Follow through to the actual affected entry path.
- Choose coherent behavior ownership and migrate every caller. Delete replaced
  code in place. No compatibility aliases, alternate codecs or second caches.
- FieldCodec has one implementation owner. Extend its declared field capabilities;
  never subclass it outside `field_codec.py`.
- Focused tests check the implementation. Before claiming usability, exercise the
  installed affected native/ACP/UI path. Extend the existing continuous saved-state
  user journey; controlled provider replies are allowed, real UI/transport/state
  are required. A fork check includes immediate opening and its first actual reply.
- Ship substantial useful checkpoints and install the paired pins after checking
  them. Deferred CI and final latency targets do not hold those checkpoints.
- Retain warm rendered bodies across tab returns within the existing resource
  limits. Scroll preparation follows velocity and destination intent, then shrinks
  when idle. Do not introduce a parallel cache to repair ownership or freshness.
- Keep a concrete checklist and report merged, installed and verified states
  accurately. Retire test processes and owned scratch; check disk/RAM before large
  work. Never replay an uncertain input or restart an active owner for convenience.
