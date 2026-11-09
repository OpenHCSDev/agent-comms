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
