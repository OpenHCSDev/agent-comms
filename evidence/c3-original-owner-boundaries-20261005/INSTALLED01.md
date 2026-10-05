# Installed01 and source correction

The once-only canonical run failed: five passed, one failed in 4.279447 seconds.
The reply case called `require_turn_lease()` on `RegistryOwner`, before its reply
workflow. This is an authored control error. The original peer lease created by
`begin_turn` remains in the failed private root; its evidence was not repaired or
relabelled. No SDK/native/provider input occurred. The five passing cases retain
only their canonical registry/goal scope.

The existing `RegistryOwner.turn_lease` property derives the exact lease from
`Thread.require_turn_lease()`. Both control call sites now use that property.
The original `AgentActivity.begin_turn` producer and `finish_turn` CAS remain
unchanged. The original NRA package trees parsed 324 production and 372 test
modules without omissions; these were the only two begin-return calls using the
wrong method. The other five controls are AST identical. Production changes
versus the reviewed wheel are zero. This source correction has not been run.

The native phase was not entered. The 7a READ grant was returned; Sch closed it
and withdrew the unactivated conditional EXEC proposal. Native inputs, localhost
posts, external/provider/public inputs and replay were all zero.

The normal original 5f Core wheel was restored. All 347 installed assets and 510
logical original records match bytes, modes and links; the 94 unique protected
keepers, 69 metadata files and ten distributions are preserved. All recorded
stage, proof, test and restoration processes are absent; groups and sockets are
empty. `whole-handback.json` binds the 18 original raw keepers. Bohr owns the
independent lifecycle closure.

Remaining acceptance: one changed certified-reply control, then the two unrun
original saved-SDK/TurnRunner localhost settlement cases after an explicitly
issued successor holder/artifact purpose. The five unchanged passing cases will
not be repeated. No Ready, UI, public or timing claim is made.
