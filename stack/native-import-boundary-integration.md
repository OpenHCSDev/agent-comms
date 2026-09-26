# Canonical import-boundary candidate — independent review required

This integrates the `5dbd6fe` API draft; it is NOT activation or combined runtime
clearance. All builds/tests used new disposable copies. Reviewed writer artifact
`3585b0f` and old `8cf666c` artifacts remain unchanged.

## Deployment and admission

Canonical preparation now copies PR77 `index.mjs`, `src`, `bin`, package/lock and
README plus offline-installed production dependencies into
`agent-comms-extensions/pi-mcp-client` inside the committed native package.
`npm ci --offline --ignore-scripts --omit=dev --omit=peer` avoids lifecycle
execution and a second unfenced SDK; bounded materialization converts only
internal regular-file aliases to independent files. No installed dependencies
are pruned. Explicit umask makes execute-bit commitments reproducible.

The SDK extension loader uses `loadApprovedExtension` and no Jiti evaluator or
fallback. Existing static host provider imports are retained so removing Jiti
aliases does not also remove provider-registration side effects. The manifest
allows the exact copied PR77 entry, with its SDK peer bound to the committed host.
Runtime installation must explicitly select this in-root package. Prior external
installations need explicit reinstall/migration; name/version matches do not
silently map arbitrary external code into approval.

Package acquisition is also gated BEFORE resource discovery, automatic missing
package installation, install/remove and configured updates. Otherwise npm/git
or lifecycle scripts could execute before an eventual module-loader refusal.
Only manifest entry files or their exact package directories are admitted;
npm/git and other local package sources are refused. The canonical wrapper
rejects self-update: publish/verify a new immutable deployment instead.

Both canonical CLI and Python compaction bridge preload synchronous Node
resolve/load hooks before bootstrap/SDK/helper modules. Non-builtin, non-file or
out-of-root module resolutions/loads are refused. Ambient Node preloads/search
paths and compile-cache directory are removed, compilation caching disabled.
The compaction helper now lives inside the pinned tree and must byte-match the
wheel/source-owned helper resource before journal initialization or dispatch.
The exec path is still non-forking; inherited authority FDs and watchdog PID
semantics are unchanged. No runtime fault knob or caller-selected allow-root was
added.

## Exact candidate identity

- Manager unchanged: `10ac30c15dd1b47b86fef4121c01d4b52b4a114cb9fcba6a909c04a3f5f88a7d`.
- Complete tree: `b6d13d86dd690817b0f30329fe75598ea3347cd38dbbeb1602f6188822d25db8`.
- Build: `26e29f3669b35ce5`.
- Extension loader: `5aa2488a9695a629a5057659161a0fadac9f6dadd6eee3b5dbd73864ddb7a0c5`.
- Package manager: `dc6f91239e8f15bad9dbdd1e912e89d43f8eb48ffe055b39593b3165cd7093ab`.
- Copied helper: `7f81d7100afdccf7d6ac3a69137ec10aab72f9c7c4d199b3e2a98cd40689f007`.

Fresh canonical pointer `/var/tmp/pr95-import-final-package`; repository pointer
`/var/tmp/pr95-import-final-stage` selects `/var/tmp/pr95-import-final-n1sORp`.
Earlier unpublished candidates/logs remain distinguishable; the final pin above
includes package-source admission, unlike the earlier module-only integration.

## Provider-free evidence and scope

`test-native-import-rpc.py` uses the exact copied canonical launcher, disposable
HOME/agent/project and actual `pi install` of the in-root PR77 package. It checks
the saved installation path resolves to that package, observes `mcp-status` and
approval-command registration via `get_commands`, and invokes `/mcp-status` as a
local extension command. Result: `user/fixture: trust_required; calls=unavailable`,
with no unapproved server marker. No project trust or MCP server approval is
written. Pi installation and MCP execution approval remain separate layers.

All fixture child processes inherit a Linux seccomp filter that kills network
syscalls; a controlled attempted loopback socket dies with SIGSYS. The positive
and negatives run successfully without attempting network activity or provider
requests. This does not rely on stripped `NODE_OPTIONS` instrumentation.
With offline skipping disabled, install/remove/startup of an unapproved npm source
are rejected by the package boundary BEFORE a configured npm-command marker can
execute. The kernel network denial remains active for these controls.

The ancestor negative plants a regular `bufferutil` package outside the committed
root. The actual pinned `ws` dependency executes its marker without the fence,
even with `--no-global-search-paths`. Under the fence, a real PR77 entry is admitted;
a separately imported committed `ws/wrapper.mjs` entry reaches the optional
`bufferutil` require, and a test-only observing hook records
`ERR_NATIVE_IMPORT_BOUNDARY` at `ws/lib/buffer-util.js`, with no marker. **We do not
claim PR77's current startup naturally imports ws**: an initial probe established
it does not, so the real in-root dependency entry is explicitly invoked. The
canonical registration test and this reached dependency test are separate proofs.
No test observer or sentinel is added to production bytes.

The 12 module contracts additionally cover admitted-extension transitive denial,
ESM/CJS/createRequire/absolute/data imports, peer identity and copied old Jiti
normal-evaluator/tryNative-fallback execution versus native-only refusal. These
synthetic admitted-entry controls are not mislabeled as canonical RPC tests.

Final native writer/coverage/journal/exclusivity/hard-context/manual/parallel/input
recovery scripts and adaptive contracts pass. Source MCP/provenance/authority/
journal/ingress suite: **243 passed, 1 skipped**. Offline sdist → wheel build and
actual extracted-wheel module/resources: **54 passed**. The orphan SIGKILL and
hung-deadline tests still exercise real native methods and all retained locks;
their barrier now resides in a separate test-only copied/pinned tree rather than
using an external helper that the production fence correctly refuses. A separate
control proves external helper execution is denied. No frozen package was edited
to inject a barrier. Logs `/var/tmp/pr95-import-final-*.log` and
`/var/tmp/pr95-import-wheel-*.log`.

## Remaining limits / required review

The complete-tree verifier and immutable deployment govern bytes; the hooks are
not an OS sandbox, protection against arbitrary same-UID changes after checking,
or confinement of trusted code's eval, addons, subprocesses or explicitly
approved MCP servers. Unsupported direct launch paths are not canonical launch
proofs. All runtime writers still need verified rollout/retirement.

Request fresh exact-byte import/package/bridge review from both designated
reviewers. The preflight review's narrow CLEAN at `3585b0f` concerns manager
retirement only; it does not clear this new tree or imply full readiness. Actual
adaptive source preparation, idle-manager discard/reopen, ACP/correction/send
admission, keyed metadata-only outbox/publication and recovery/E2E remain OPEN.
No activation, installed-package mutation, live-session write, provider request,
summary broadcast or UNKNOWN replay occurred.
