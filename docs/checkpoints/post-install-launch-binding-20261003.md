# Bind target launch only after the owning installation

Failed349 is preserved: receipt preflight-complete SHA
bbc0ebd043dbcdb2977e44d4d896bac6680415fc625d7312325c83d866a7098f,
19 owners, no fence/stop/index/originals directory. Parent verified43 recovery
original hashes and five current553 links unchanged. Do not repeat this receipt.

The source reader for checkpoint admission/rebuild is the original installed
writer. Target code cannot certify that old checkpoint before conversion.
OwnerLifecycle.pin_private_nk_launch retains a launch capability and validates
its root through the bus barrier; the publisher currently calls it too early.
Request.environment is computed before fence. Handoff.launch chooses the pinned
native entrypoint and OwnerLifecycle rederives the private pair at actual launch.
Thus admission does not need target pin validation against old checkpoint bytes.

Use existing StoppedOwnerInstallation for install -> prepare target launch ->
launch. Index/routing supply their binding through the existing installation
member; outer publishers own their final target binding after all nested installs.
Nested after_stopped does not perform another binding. Remove both publisher
pre-pins and duplicate complete orchestration. Keep OwnerLifecycle/private pin
validation strict. Bootstrap/preserve members inherit outer publisher binding;
standalone index/routing inherit their own binding. No new reader/type/store,
manual stop, schema skip or provider/input retry.

Before editing, map all declarations/calls/inheritance with the existing NRA
Package parser across Core production/tools, Toad consumers and parent source
activation. Read all candidate calls semantically; AST does not prove dynamic
resolution. Implement the complete family, then batch final focused sanity and
one original installed old-checkpoint -> target rebuild/retained launch control.
No repeated native/provider/UI journey. Current553/previous334 stay immutable.
Merged554 joins normally; its qualification remains unchanged. Corrected receiver
and NEW operation follow only after this source batch; failed349 stays frozen.
