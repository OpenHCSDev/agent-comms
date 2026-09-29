# Actual native channel reply roundtrip regression

Production RED base: main da2b43d4. Scope tests only; parent owns production fix.
Own persistent ~/wt/comms-native-reply-roundtrip-20260928 and noneditable installation.
Actual pinned native bundle native-current-5fdef596596173bd; local-only HTTP model.

Two production owners are registered and started normally, with channel membership.
The questioner attaches via actual CommsClient/socket, then publishes an ordinary
channel question naming answerer. Answerer executes actual native FULL and publishes
ROUNDTRIP_NATIVE_REPLY. Questioner must automatically receive it in actual native
bounded triage; provider chooses IGNORE, preventing recursive reply work. No manual
inbox/drain, participant insertion, claim creation, user prompt or native event stub.

Assertions preserve two distinct durable native inputs/sessions, source reply content,
ACP proven cursor feedback, two total provider requests, no third channel message,
no duplicate/replay/ping-pong beyond watcher fallback interval, owner cleanup.

RED receipt: B actual native request and actual reply reached; sender observation
must fail on current production. See red.log for executed result (not a green claim).
The post-fix receipt must exercise remaining assertions; these are not proved by RED.

Latest 22:16 skills applied: AGENT-8 production paths, AGENT-3 no duplicated legacy
fixture matrix, BOUND-1 shared typed ACP decoder, TIME-9 no adapters/aliases. New test
has zero BooleanChainTerms (no >=4-term BooleanOp). No production/live changes.
