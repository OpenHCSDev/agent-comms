# Connector refusal and candidate maintenance repair

Owner: pi-connector. Branch: fix/connector-refusals. Requested by claude-code.

## Source decision

At base 2be439d1e, DirectorCaller raises a bare ValueError for directive text and
expired reviews. ToolsCallMessage catches only UnregisteredThreadError, so aiohttp
answers 500 instead of an MCP tool error. RuleReviews already owns review age,
the 20-minute limit and next-step wording; do not derive other standing rules
before refusing a send.

CandidateCheckpoint validates root/device/inode, prefix size and tail digest.
An install/carry can replace bus.jsonl while retaining its disposable candidate
SQLite file. FirstBuild resumes existing checkpoints and refuses the changed
inode. The post-commit worker does not handle ProjectionUnavailableError, so it
exits before candidate and route-count catch-up. Original bus delivery remains
canonical; the optional candidate supplement and this worker's route preparation
stop until another append schedules maintenance (and then fail again).

Repair owners: explicit ToolRefusedError at intentional refusal sites; only the
MCP tool boundary encodes it. Invalid input is distinguished from implementation
exceptions. DirectiveText owns its raising check. Candidate maintenance selects
resume/rebuild within WakeCandidateIndex's captured source and revision-checked
batch, not a thread restart or retry. Read-only pages and strict resume still
refuse stale sources; malformed schemas still require explicit reset.

## Source census

Used refactor-audit's Package.load/ParsedModule with Python 3.14, all 379 Core
production modules, zero parse omissions. Enumerated every class declaration and
all imports, declarations, writes, checks and references in the connector,
review and candidate families. Dynamic attribute binding remains syntactic/OPEN;
read actual referenced sites before choosing owners. No dependent external
package owns these local exception or projection decisions. The NRA checkout's
Python 3.12 cannot parse Core's generic defaults; reused refactor-audit rather
than treating that failed scan as coverage.

Scratch: /home/ts/.cache/agent-scratch/pi-connector (owned by pi-connector;
source census and bounded scratch-bus connector verification only; no user data).

## Delivery checklist

- [ ] Implement typed refusals and owner-selected candidate rebuild.
- [ ] Run a real connector subprocess and curl MCP calls on a scratch bus.
- [ ] Expired review response includes age, limit and action, below one second.
- [ ] Replace scratch bus source; observe the original maintenance thread finish.
- [ ] Focused checks under timeout 300 and Python -X faulthandler.
- [ ] Separate debt-ratchet call; publish PR without merging/installing.
