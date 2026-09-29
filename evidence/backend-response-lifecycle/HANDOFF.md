# Backend response lifecycle — implementation checkpoint

Dependent on ready PR288/d155c6ff. Original S2 G4 and S7 ownership closure.
Read original S1/S2/S7, round2 rules, full TurnSession, PiEvent/PiCommand,
StatsRequest/InputForwarding, PiRpcChannel/PendingRequests and child owners.

Response identity belongs to command capabilities, shared event progress to
PiEvent.consume, prompt acknowledgements to Prompt/InputForwarding, interruption
to its command/events. TurnSession retains the actual identity-invalidation
reaction. The backend's three central decision procedures are deleted.
StatsRequest now holds the actual canonical response futures; completion/failure/
busy derive from those responses. Delete its duplicate response IDs, ID set and
mutable completion flags. PiRpcChannel.track is the canonical PendingRequests
operation shared by encode and stats; no second broker or registry.

No store/schema/native package changes. ChildProcess supervision unchanged.
Dalton owns the29 baseline fixtures; Boyle stores and Tesla/CarverToad untouched.
Focused first68pass includes real local child-pipe settlement races plus
protocol/phase declarations; actual pinned CLI acceptance in progress. Not ready
for deployment until actual installed/native receipt. Parent owns deployment.
