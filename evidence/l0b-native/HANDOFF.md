# L0B native-only closure — implementation checkpoint, not ready

Pascal owns PR234 / ~/wt/comms-s10-pi-boundary-20260928.
Source includes parent2299ff1161 integration and S10 startup UUID-parent correction7cd8702.
NRA skill + full audit sections1–4 read before code. Prior complete global NRA
coverage/raw-record leads are in the supplied audit section7; no clean-scan claim.

Implemented:
- Delete backend.rpc_args_for, raw task-as-argv/text stdout engine and image split.
  TurnSession owns NativePiRpcLaunch. Managed configuration resolves approved
  current route/stack/direct pinned CLI, verifies package, validates RPC arguments,
  isolates Node imports and preserves configured model/settings. No basename inference.
- Migrate owned_turn/turn_runner/acp/session_lifecycle and InputDrain.run_owned_input;
  direct inputs always create original disposition. TurnInputs steering consumes
  the same launch environment (caught/fixed by real-pipe tests).
- Delete Participant, ParticipantEventConsumer and their independent ACK/poll loop.
  Published agent-comms-agent command now uses worker.main and canonical owner
  runtime; headless adopts an existing identity/project or creates in invoking cwd.
  Explicit semantic correction: tools use owner's worktree, never sender's.
- Reopen accepts verified package directly; package lookup belongs NativePiRpcLaunch.
  S9 owns paired manual/bridge/adaptive caller changes, agrees no execution classifier.
- Delete obsolete raw-text/classifier/Participant tests; preserve native failure,
  input, ACP metadata/queue, cancellation and actual subprocess contracts.

Current evidence:
- binding-first.log timed out after failures; binding-failure.log isolated missing
  TurnInputs.env_extra migration. Repaired:binding-fixed.log73passed13.72s.
- launch-first.log caught missing-command lookup consulting active route first;
  reordered lookup to report original missing executable before route inspection.
- launch-current.log3pass, native preflight fixture used --no-session and therefore
  correctly lacked persisted-input capability. Fixed fixture to isolated session dir.
- native-headless-first.log actual pinned native preflight PASS without prompt;
  headless collection currently blocked by Darwin manual_compaction importing the
  now-deleted rpc_args_for. His migration is in progress; exact API supplied.

Remaining before readiness:
1. Integrate Darwin manual/bridge/adaptive patch; no compatibility function.
2. Focused headless/runtime/stdio, original-input failure/queue/cancel paths and guards.
3. Build own wheel and exercise local installed artifact, no live install.
4. Publish final deletion counts/receipt; parent owns whole-step quiet activation.

No live mutation/restart, paid provider, new environment/model/worker. Actual Pi
checks use existing prepared package and only GetState or deterministic loopback.
Own test fixtures are disposable; failed evidence retained. Parent owns InputDrain
queue/history/removed queue-restored projections and paired Toad.
