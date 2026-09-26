# Native package provenance checkpoint (not activation approval)

The canonical preparation **code** now applies the production writer, journal,
writer-coverage and writer-failure-state patches in that order, then assembles
the in-root import boundary, copied helper and PR77 production package. Only disposable builds were made for
this checkpoint; no installed package, running owner or user session was changed.
Adaptive runtime entrypoints and publication coupling remain unfinished.

## One package commitment

`pi-native.sha256` retains selected per-file diagnostic checks and adds one
`# agent-comms-native-tree-v1 <digest>` commitment. Its complete bytes still
select the build directory, so changing the tree pin changes the build identity.
The current tree digest is
`b6d13d86dd690817b0f30329fe75598ea3347cd38dbbeb1602f6188822d25db8`;
build directory suffix is `26e29f3669b35ce5`. The SessionManager is
`10ac30c15dd1b47b86fef4121c01d4b52b4a114cb9fcba6a909c04a3f5f88a7d`.
The import-boundary candidate supersedes `3585b0f`'s `7d16eb01…` tree without
changing manager bytes; see `native-import-boundary-integration.md`. That writer
correction superseded the `8cf666c` checkpoint's `4a688172…` tree /
`41a94b37…` manager and require fresh review. See
`compaction-writer-failure-state-corrections.md`; narrow prior packaging CLEAN is
not independent clearance of the new artifact or of import-resolution closure.

`src/agent_comms/native_package.py` hashes a deterministic preorder of sorted
paths. Each UTF-8 JSON record plus newline binds node type, relative path and
execute bits; file records also bind size and SHA256 content. Dependencies,
package.json resolution metadata, native binaries/resources and hidden entries
are included, not merely SessionManager or selected modified files. Timestamps,
absolute installation paths and inode numbers are not part of the commitment.
File identities are checked while reading to reject observed concurrent changes.

Verification rejects links, special files, untrusted ownership, group/other
writability, excessive inventory/depth/bytes and mismatching tree contents. Work
is bounded to 30,000 discovered entries, depth 32 and 512 MiB; directory listings
are capped while enumerating, before sorting or recursive descent. No mtime
cache, mutable success marker or caller-provided digest grants acceptance.

Preparation checks npm's source aliases stay inside the package and point to
regular files, then materializes independent copies (no symlinks or hardlinks)
before applying any patch. This also prevents a copied alias redirecting a
patch into the stock installation. The completed package is checked before its
new directory is published. An existing changed build is refused, never repaired
or overwritten in place. Fresh installs with different transitive dependencies
must fail rather than borrowing approval from the matching Pi version number.

This exact artifact was built and tested on Linux x86_64. Other dependency or
platform variants need their own reviewed commitment; there is no unpinned
fallback. The external commit bridge still separately requires Linux pidfds.

## Every supported entrypoint checks

- Canonical `prepare-pi-native`: selected-file checks **and** complete tree check,
  both for new staging and an already-existing target.
- Canonical `pi-native`: complete tree verification before Node executes the CLI.
- `OwnerCompactionCommit`: complete verification before journal initialization
  and again before each commit/reconciliation dispatch. There is no longer a
  separate, weaker manager-only bridge pin.

The Python wheel carries the manifest and helper in `agent_comms/_native` via
Hatch force-includes. Their source files are also included in the sdist. Resource
selection uses those installed files, or the explicit `src/agent_comms` source
layout; it never searches neighboring directories for an arbitrary stack.
An offline sdist-to-wheel build and extracted-wheel execution verify this path
without installing or modifying the live Python environment.

Ambient `NODE_OPTIONS` / `NODE_PATH` / `NODE_COMPILE_CACHE` cannot inject code into these native entry
paths. Preparation and the CLI wrapper remove them; the non-forking commit child
uses `env -u` before exec of Node, preserving the exact watchdog PID and retained
FD lifetime. Node is passed `--no-global-search-paths`. This deliberately also
removes ambient Node tuning flags. The managed-project cwd override is preserved
by copying `pi_project_bootstrap.mjs` into the pinned package and importing only
that verified copy from the CLI wrapper. Both entrypoints preload the in-root
import boundary first, and disable compilation caching. The commit helper is
also copied inside the root and must match the packaged Python-owned resource;
it needs no CLI cwd bootstrap. Parent environment is not changed by a commit.

## Threat and rollout boundary

This is content provenance, not a sandbox against arbitrary same-UID programs,
untrusted OS/interpreter binaries, malicious native CLI arguments/extensions or
an installer rewriting files after verification. Python owner/helper code and
the system interpreters remain trusted. Approved PR77 installation must select
the exact copied in-root package; project/MCP execution trust is separate.
Arbitrary user programs writing
session JSONL outside the native API are not made cooperative by a digest.

Disabling global search paths and hashing the tree alone did not close ancestor
`node_modules` fallback. The successor adds synchronous resolution/load fencing,
native-only manifest extension loading and pre-install package-source admission.
See `native-import-boundary-integration.md` for executable positive/negative
controls. Fresh exact-byte independent review is still required; neither this
mechanism nor its limited tests claim an OS sandbox or complete runtime readiness.

Package directories must be immutable **by deployment policy** after publication.
Prepare a new directory and switch only new processes; never patch a running
package. Crucially, old workers may still use an earlier, unfenced package.
Preparing this copy alone does not retire them or authorize adaptive mutation.
Runtime integration must close/reopen persistent managers against the verified
build and exclude conflicting old writers before activation. Legacy/torn native
sessions now fail closed; explicit safe recovery remains a separate gate.

## Earlier provenance checkpoint evidence

The current import integration has **243 passed, 1 skipped** source tests and
**54 passed** extracted-wheel tests plus real network-denied canonical PR77 and
ancestor controls; see the integration report. The following results retain the
historical `8cf666c` baseline rather than relabeling old evidence.

- Source-tree complete-package/unit and native authority/journal/ingress suite:
  **227 passed, 1 skipped**. Includes dependency/metadata/hidden-entry tampering,
  links, resource bounds, concurrent read changes, pre-journal refusal and
  ambient preload injection. Extracted-wheel package/real-native bridge suite:
  **51 passed**, with module origin explicitly asserted inside that wheel.
- Canonical preparation ran from stock into an isolated repository under
  `/var/tmp/pr48-canonical-prepare-FjWKoI`, then independently verified on repeat.
  This is not the installed stack path.
- The actual resulting CLI prints `0.85.1` with an isolated HOME and malicious
  ambient preload present; the preload marker is not created.
- Actual unlisted dependency package.json drift is rejected by both the launch
  wrapper and existing-target preparation; evidence is left intact. The
  disposable test mutation was restored explicitly, not by either entrypoint.
- All seven native writer/journal/exclusivity/hard-context/manual/parallel/input
  recovery scripts and the adaptive contracts pass on the canonical staged
  result. The native writer suite includes 19 adversarial controls.

Logs are `/var/tmp/pr48-provenance-*.log` and
`/var/tmp/pr48-canonical-{prepare,prepare-repeat,version}.log`. Full runtime,
ACP correction admission, keyed metadata publication/outbox, operator recovery
and end-to-end activation review are still required.

The earlier independent CLEAN remains narrow: prior SQLite post-unlink
durability and Linux orphan deadline at `5f50fe7`, also checked at byte-identical
`90c4d57`. It does not approve this new provenance slice. Final recovery policy
must retain the simultaneous watchdog+owner-crash limitation and the fact that
uninterruptible kernel I/O can delay exit while exclusions remain held.
