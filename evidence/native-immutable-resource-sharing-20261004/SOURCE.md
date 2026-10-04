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

## Complete declaration/consumer evidence

Before and after Python consumer output uses the existing refactor-audit Package.load parser across Core, stack Python, canonical cutover tools and Toad dependency roots: 663 modules, zero Python parse omissions. After output records 339 declaration/import/decision/consumer source sites; it includes related private-state checks that remain intentionally single-link.

The initial system-Python lookup lacked JS/Bash grammar packages. The existing installed dependency holder has those parsers, so the original and implemented tracked stack/experiment JS/MJS and Bash sources were parsed without installing dependencies: 47 files per revision, zero parse-error modules. This fills the earlier explicit non-Python omission; compiled dependency bodies in immutable handed artifacts are outside this source enumeration. Dynamic import, subprocess and callable-factory resolution remain behavioral boundaries rather than AST proof.

Production change: 75 additions / 7 deletions across native_package, the sole build script and import fence. Replaced independent-file-only decisions are deleted at those owners; mutable extension binding/private state retain their separate contract. At that historical source checkpoint, compiled sharing and SDK launch were pending. The later compiled01 result below supplies that scope; no disk-reclamation claim.

## Main integration and original remaining resource gate

Normally joined merged610e1c399d7/currentmain, including603/605/607/609. The three608 production members are byte-identical to reviewed9144; no semantic patch or repeated source checks. The existing609 acquired tracked factory consumes the same original _trusted_package result before claim and retains it for first/later stages. No new trust/cache/flag family.

Remaining qualification is one changed compiled resource path: a new matching-fence sealed deployment, genuine readonly resource reuse from a new same-fence shape/content-verified donor, whole-tree trust plus actual SDK import/read, and writable-shared/content-change refusal in that affected batch. Old89/f3/d5/2ea retain the old fence and are forbidden donors; their original bytes are untouched. No artifact was built or altered by this source join. Existing compiled sources are not source-AST proof, and historical663/47 parsing is not relabeled as currentmain coverage. This was the notReady boundary at4b65. The compiled01 gate below now closes it. Functional416/417 remains independent and its owned run is untouched.

## Compiled resource qualification complete

ONE current stock/cache assembly produced the newly sealed native086d deployment. Exactly one compiled member changed relative to immutable610/2ea: dist/agent-comms-import-fence.mjs. All 19,190 regular files were sealed read-only before publication; the unchanged canonical prepare-pi-native verified the new whole-tree pin. Old89/f3/d5/2ea retain their original whole-tree commitments and independent file names.

The validation receiver used directories and hardlinks to that NEW shape/content-verified same-fence donor only, with no second SDK payload or assembly. Existing share_native_resources reported 19,190 shared files / 138,420,949 logical content bytes. Receiver-exclusive regular-file allocated bytes were zero (directory metadata excluded). The retained single-link canonical package occupies 194,633,728 regular-file allocated bytes after receiver retirement; zero old payload bytes were reclaimed by this qualification.

The actual SDK imported through the shared receiver and passed the original authored capture/root/exact-child/mixed recorded-reader contract, including invalid coordinates/transformed input and unchanged journal bytes. One SDK execution, 0.966 seconds, no network/provider/prompt/public input. This is an SDK sharing/import/read acceptance, not a speed comparison, configured model publication, physical UI, whole-turn or S4 result. Existing import-fence controls passed 16 cases and the bounded serial whole-tree/refusal controls passed 13 cases. Mutable journal and extension-source single-link contracts are unchanged; private-state file counts were not measured before authored scratch cleanup.

Two private harness negatives remain original: first receiver trust refused before SDK because the private umask changed directory execute bits; correcting only mkdir mode preservation enabled the same-tree receiver. The later pytest command refused its unavailable configured xdist arguments before collection; only the remaining controls ran serially with original tests. The accepted SDK was not repeated. Overall original attempt receipt remains FAILED because of that harness argument refusal; READY.json binds its independent SDK pass and the separate final serial-controls pass without rewriting either raw result.

All owned SDK/test processes joined; receiver and authored scratch retired. The NEW canonical artifact, original concise recipe/terminal/source/pin/control receipts remain. Functional416/417 imports are untouched; no sharing code/pin was folded into their D5 cohort.

Native manifest: 086d511f2026b10d2cdb09380026d9503b0461e3a9998048902fe3aee5671872
Native tree: dbc88ed9231d1275da3ce218aa8919a247e41ce1f010ce765e7ac0960193f4ac

Ready at compiled resource/import/read scope. Parent reviews/merges before future installed/public consumption. Original full goal and unrelated model/UI/performance/S4 scopes remain open.
