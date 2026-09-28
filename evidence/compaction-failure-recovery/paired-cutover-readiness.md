# Paired cutover readiness

Core85363055 is installed in the staged candidate, not the live installation.
The focused T2/codec/selected-write/queue batch passed35 tests. Its process
returned1 solely because the project-wide85% coverage threshold was applied to
this focused batch (47.07%); this is not a CI or installation hold.

The first readonly installation preflight passed. A repeat correctly refused
while five ACP clients were present; all five subsequently exited. The final
supervised repeat completed with exit0, four idle owners and no attached
Toad/ACP client. See paired-cutover-preflight.log. No runtime reset, route write,
owner restart or symlink swap has been executed. A previously committed empty
JSON redirect was removed; it was not an acceptance receipt.

The operator must run with the candidate interpreter and existing active route.
It excludes live UI/ACP clients, closes admission, verifies idle identities,
retires the owners, resets only the two declared runtime stores, publishes the
paired package/launchers, restarts the same owners and checks their configuration,
participation and unchanged native history. On an incomplete switch it retains
the maintenance phase and a result receipt; it never replays an input.
