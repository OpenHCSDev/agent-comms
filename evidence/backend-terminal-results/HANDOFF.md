# S2 terminal-result ownership — implementation checkpoint

Base: merged303/main664623a3. Parent owns integration and installed acceptance.
No live install, replay, native package edit or runtime cutover.

TurnOutput owns output buffers, provider error/privacy, terminal stop, diagnostic
metadata and the winning TurnFailure. Existing InputMissing/FinalStopMissing/
QueuedInputMissing declarations own detection and presentation through abstract
TerminalFailure; membership and precedence derive from the existing family.
InputForwarding, native identity, StatsRequest and ChildProcess remain actual
proof authorities, read directly rather than copied into a second record.

Deleted backend initialize_output, record_failure/fail_reason and its repeated
terminal failure branches; direct event/command/input/watchdog/preparation
consumers use the owner. No aliases or compatibility APIs.

This is a code checkpoint, not readiness. The recorded external-protocol child
suite is running and has two failures under investigation. Actual native local
provider acceptance and final audit are pending. No paid provider calls needed.
