# Native immutable resource sharing

Existing native_package owns bounded whole-tree content provenance. prepare-pi-native owns the sole compiled package publication; native-import-fence owns every SDK import. A hardlink is currently rejected even when it only shares identical frozen code. The existing tree commitment already records path/type/execute-mode/size/content rather than link count. Link count and ctime are also compared during hashing, so attaching another immutable name currently causes a false mutation refusal.

Migrate this complete artifact family: accept only readonly shared regular resources, preserve no-follow/owner/bounds/content/inode checks; have the existing builder seal resources only after all patching and reuse only a resource donor carrying the same newly reviewed import fence. Current89/f3dd/d5 artifacts have the old fence and cannot be donors. Private journals/settings/auth/source binding remain distinct single-link facts. No handed artifact changes.

Source lead: IMPL-13 (one filesystem boundary implemented at different levels). Before/after AST consumers and parse omissions are recorded for the bounded family; these do not prove dynamic execution. Checks come after the complete change. No provider/native schema or public change.

## Working implementation

The existing package verifier now accepts a shared regular resource only when it is read-only; writable hardlinks, foreign ownership, symlinks, special files, byte/entry/depth bounds and content/inode checks remain enforced. Link-count/ctime changes from acquiring another immutable inode name are excluded from the read-only content identity. The existing import fence uses the same filesystem contract. External extension source binding and every private mutable-state single-link check are unchanged.

The sole prepare-pi-native producer seals the NEW patched package after its final import-boundary patch and before verification/publication. It shares only equal bytes, execute mode, size and filesystem from a fully shape-verified donor carrying the exact new read-only fence. Changed compiled resources remain independent. Existing89/f3dd/d5 artifacts have the earlier fence and cannot be borrowed or modified. Counts describe logical shared content, not measured reclaimed filesystem bytes.

Mendel owns selected acquisition consumers in native_pi.py/coordinated_runtime.py/tracked_turn.py. His existing acquired launch factory retains the original whole-package result through the execution; standalone launches acquire normally. No additional cache, trust flag or class is introduced here. Shared-file preparation and acquired-launch reuse are distinct owned lifetimes.

## Completed source checks and honest limits

Original source package batch: 26 passed; two existing compaction-journal controls could not import ACP in the system Python (ModuleNotFoundError: acp), before reaching their assertions. The original failure log is retained; no dependency install, mock or product workaround. All new sharing/content controls passed. Existing real Node import-fence contract: 16 cases passed, including read-only shared SDK code loading and writable shared code refusal. Bash producer syntax and Node import-fence syntax passed. This is source/filesystem/module qualification, not a compiled SDK deployment, installed live path or readiness claim. No new native artifact/provider/public run.
