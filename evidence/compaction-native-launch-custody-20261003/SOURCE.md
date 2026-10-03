# Compaction borrows its acquired native launch

Current same retained child performs selected/manual/adaptive summary. Each of
those three original production callers nevertheless constructs a new
NativeCompactionWriter from package Path; its constructor verifies the entire
immutable native tree again. The original NativePiRpcLaunch already owns that
acquisition. This happens before selected summary, not evidence for the observed
4.972-second post-summary gap or 98.010-second provider duration.

Extend the existing writer's acquisition to consume the actual original launch
resource when the existing bridge is opened under that child's custody. Standalone
writer construction keeps fresh package verification. Both paths still validate
exact packaged commit helper/import assets before journal construction. No
prior-success boolean, path/mtime cache, package registry or native change.

Migrate all three production bridge callers together. Preserve original child,
session/source/revision/commit/input fences and joined worker lifetime. Acquired
launch is an optional borrowed resource, not a None-valued lifecycle state.
Boundary: complete native deployment immutable-by-policy, not a same-UID sandbox.

Existing NRA/refactor-audit Package AST covered726 Python src/tests/tools modules,
zero parse omissions; source-before.json contains49 relevant calls/declarations.
Actual launch/retained custody/writer/bridge and standalone negative consumers
were read. Calls through aliases/dynamic dispatch are not proven by syntax alone.
Standalone preparation and token readers are separate acquisitions; this batch
does not silently borrow them. Related pattern IMPL-5, repeated implementation.

Final checks detect invalid independent artifacts, wrong borrowed artifact and
helper drift before journal creation, plus installed original source compaction
writer custody without new provider input. No repeated full provider journey,
new environment/worktree/native build or public/default changes.
