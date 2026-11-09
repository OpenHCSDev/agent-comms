# P23 InputOrigin contribution

Use `input-origin-v3.plan.json` (SHA256 `824b75784e6087a90e9aa5006de5f5b1a5eb3ca72d27ecbb85339f87e6e98b20`). All earlier versions are superseded. V2 is structurally wrong: task_sources imported the I/O implementation and therefore executed RegistryStore during Message bootstrap. Module qualification did not defer those imports (friction's source-backed correction `dff71fa43cf7`).

This is an artifact-only proposal: 3 stages, 35 existing native operations. No product file was edited in friction's tree. It has **not** been natively simulated, applied, cold-import qualified, or consumer-tested.

## Composition

Compose with friction's existing InputLookup and InitialOrigin relocation/fact-binding prefix. The retained-task-facts concrete Message/InputAttempt/StoredInput bindings must refer to their original declarations through ordinary module bindings, rather than eager class imports during partially initialized input modules. Friction owns those bindings. Field types stay the original concrete records. NativeInputConstraintPin's new callback annotation uses the prefix's `input_attempt` module binding in task_sources.

1. Relocate original UserThreadLookup/FoundUserThread/NoUserThread declarations into `registry/user_thread_lookup.py`, updating consumers with the existing native relocation operation. No new selection policy.
2. Create state-free `input_ingress.py` contracts and add them to existing providers. RegistryPrivacy inherits its original namespace/root abstract declarations from InputRootAuthority. Providers retain their implementations and concrete fields. Native base operations preserve independently changed provider headers. Comms binds the original InputDispositions constructor once; public_text constructs that exact store at call time with comms.root. No cache or forwarding method.
3. **Separate API and implementation.** Task bootstrap imports only generic InputPublicationRead from `input_publication_read.py`. That API imports abc and the existing lower input-ingress contracts, not the implementation or a store. Actual BusReader inherits InputPublicationSource, which inherits InputPublicationRead with the original concrete Provenance/StoredInput types. Only the implementation imports and executes store modules. It owns the original privacy-then-input-lookup I/O sequence and returns the original privacy object and StoredInput in a tuple. The caller still performs row.origin.require_human() before privacy.namespace, followed by its original author/recipient/project checks. PublicRegistry's namespace refusal does not move before lookup or origin validation.

## Preserved authority path

HumanInputOrigin.capture still takes the first authority cut, verifies active owner/exact claim, reads the original namespace, calls user_identity(owner.worktree), takes the second authority cut, checks namespace continuity and active owner/exact claim again, resolves the current original user, and constructs the same fields from that cut.

require_ingress still checks attached controller, current root, exact claim, registered author, active owner, and project/goal scope in the original order. PresenceSnapshot and TaskRegistry alone do not supply that acquisition contract.

## Evidence and limits

`geometry-v3.log`/`.exit` record proposal-only geometry and body preservation. They are not native simulation or runtime proof. MRO, original concrete bindings, complete import closure and consumer journeys belong in the existing combined qualification run.

The remaining bootstrap question is checked there: InputPublicationSource binds original concrete generic arguments on the BusReader implementation side. That implementation must never become a dependency of the task bootstrap API. No source-hidden or cold-import success is claimed here.

## Checkpoint and package rebase

This branch saves unfinished artifacts, not product or installation success. The earlier artifacts were authored against friction's pre-package-move snapshot and must not be replayed unchanged after its main rebase.

For pushed replay `1c532ae3a83094b4a59438e0500a2692e33ab128`, use the supplementary `packaged-ingress-namespace.plan.json` (SHA256 `c91678a74966c34a5ab52a3942ab955c87bf64fbfda09cfb9247df4b8880e0f1`). The earlier input-store binding change is already present there; it must not be reapplied. Two module aliases still name removed root paths: `input_disposition.py:12` imports `agent_comms.bus`, and `execution/input_origin.py:207` imports `agent_comms.retained_task_facts`. This supplement restores their actual `messaging.*` owners without changing aliases, any class/method AST, or I/O/check ordering. The pinned-source check exits 0; no native apply or new cold run is claimed. Compose these repairs into friction's component batch, not a separate check lap.

Freshly fetched main `33af0b93d` declares Bus/BusReader in `messaging/bus.py`, InputOrigin/InputProvenance/HumanInputOrigin in `execution/input_origin.py`, and RegistryPrivacy in `registry/private_registry_guard.py`. InputDispositions still lives in the existing root `input_disposition.py`; its Bus binding correction must name `agent_comms.messaging.bus` on that main.

New declarations from this contribution belong in packages: `input_ingress.py` under `execution/`, and the low `input_publication_read.py` API plus `input_publication_source.py` implementation under `messaging/`. The original user-selection relocation is already under `registry/`. Friction owns the integration rebase and composition; regenerate the file/module identities against that source instead of editing its tree from this checkpoint.

`capture_import_graph.py` runs claude-a's supplied map once and rejects input changing during capture. The retained fixed-source result is 145 + 8 modules. Its full members/edges stay in scratch `import-graph-current.json`; the smaller committed component manifest identifies the input and script. These are module-import leads, not proof of premature name reads or startup success. Use the existing native `promote_guarded_import` for eligible guarded imports and honor its cycle refusal; move the original declaration first rather than reproduce an unsafe hoist with patch_target.
