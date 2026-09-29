# S2 G4/G5 catalog discovery closure

Owner: Wegener. Parent owns live installation. Based on merged311.

## Checkpoint: implementation, verification in progress

ConfigOptions owns native catalog queries; its Model/ThinkingLevel declarations own catalog projection and fallback. PiRpcChannel owns one-request correlation via existing PendingRequests. BoundedRun owns deadline/child cleanup. Removed both backend discovery loops and the backend Model declaration; callers now use their existing configuration owner. No input/send-boundary or event behavior changes.

Next: actual pinned Pi catalog/auth refresh/cleanup checks; transport wrong-response, EOF/cancel and new-command checks; affected configuration callers; full-context NRA audit. No provider input, live install or replay.
