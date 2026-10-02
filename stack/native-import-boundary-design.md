# Native import boundary v1 — API design and prototype history

The successor now integrates this API and package-source admission into new
canonical bytes. See `native-import-boundary-integration.md` for current pins,
real installed-PR77/ancestor evidence and open review/runtime gates. The draft
checkpoint and results below describe the preserved `5dbd6fe` stage.

Baseline is PR95 `3585b0f`, manager `10ac30c1…`, complete tree `7d16eb01…`.
**Those artifact bytes are unchanged.** The new boundary module and manifest in
this commit are not yet copied by canonical preparation or loaded by either
production entrypoint. They do not close the current import-resolution gap.

## Authority and API

- `native-import-fence.mjs` will be copied to
  `dist/agent-comms-import-fence.mjs` inside the complete-tree committed package.
  Its deployment root comes only from its own canonical module location.
- `native-import-manifest.json` will become `dist/agent-comms-imports.json` in the
  same commitment. Schema v1 has exactly `version`, `extensionEntries` and
  `peerAliases`; no environment/CLI-supplied allow roots or expected digests.
- The only initial extension entry is
  `agent-comms-extensions/pi-mcp-client/index.mjs`. PR77 source and **production
  dependencies only** will be copied into that immutable subtree. Its SDK peer
  maps to the host's committed `dist/index.js`, not an unfenced dev/peer SDK copy.
- `loadApprovedExtension(absolutePath): Promise<defaultExport>` canonicalizes the
  selected path and requires exact manifest entry membership. It uses native
  ESM import only. SDK `loadExtensionModule` must call it instead of Jiti; native
  errors propagate without a Jiti/CJS evaluation fallback. Version 1 manifest
  entries are `.mjs`; rejecting an unlisted/TS entry is NOT counted as proof of
  transitive dependency fencing.
- Synchronous Node `registerHooks` checks both final `resolve` URL and `load`
  URL for ESM, dynamic import, CJS and `createRequire`. Builtins are allowed;
  non-file protocols and files outside the committed root are denied. Files
  must be independent regular files. No deregistration handle is exported.
  Peer aliases apply only to importers beneath the listed extension package.
- Error code: `ERR_NATIVE_IMPORT_BOUNDARY`. Resolver/module-native failures can
  retain their own codes. This API does not claim those errors are compaction
  outcomes or that they authorize retries.

## Required production wiring (still TODO)

1. Prepare a NEW package: materialize the bounded PR77 production tree; copy
   boundary and manifest; replace the SDK extension-loader Jiti call; copy the
   trusted compaction helper into the same deployment root. Recompute all
   selected-file and complete-tree pins. Do not alter the reviewed old package.
2. Both `stack/bin/pi-native` and the trusted Python compaction launcher must
   verify the complete tree then preload the boundary **before** bootstrap,
   SDK modules or helper entry code. Keep the non-forking exec path and inherited
   authority FDs/watchdog PID unchanged. Helper/package-resource identity must
   remain coupled; copying a helper must not bypass wheel/sdist provenance.
3. Continue stripping ambient Node preloads/search paths; disable ambient
   compilation-cache use. No caller may inject another preload/allowlist.
4. Publish exact successor code/pins and rerun writer/authority/watchdog and
   source/wheel suites. Direct arbitrary Node launches are not a substitute for
   the canonical verified entrypoints; retire unsupported old runtime writers.

The complete-tree verifier is the authority for bytes and inventory, not this
path hook alone. The immutable-deployment assumption remains explicit: this is
not an OS/JS sandbox, protection from arbitrary same-UID mutation after
verification, or confinement of trusted code's eval/native addons/subprocesses.
Approved MCP server execution still has its independent trust/approval boundary.

## Fixture plan and current evidence

`test-native-import-fence.mjs` currently runs **12 isolated module contracts**
when `PI_NATIVE_PACKAGE_DIR` supplies the reviewed package's Jiti dependency.
The vendor is copied into a disposable directory; evaluator caches are disabled.
An ancestor sentinel executes with the old no-global-search-paths flag alone,
but not with the draft ESM/CJS/createRequire/absolute/data-URL boundary. An exact
listed entry imports the committed host peer successfully; an unlisted entry
is refused. A listed entry actually reaches an unapproved transitive import and
is denied without marker execution.

A separate listed entry raises a native-evaluation error but can execute an
ancestor import under the CJS/Jiti evaluator. Both copied Jiti's normal evaluator
and `tryNative:true` fallback execute the sentinel; the draft native-only API
propagates the native error without fallback or marker. This is evidence of
fallback removal, not a claim that Node hooks intercept Jiti's private evaluator.
Log: `/var/tmp/pr95-import-fence-module-tests.log`.

An offline `npm ci --omit=dev --omit=peer --ignore-scripts --offline` in a
DISPOSABLE copy of the PR77 package/lock succeeds; production dependencies are
35 MiB and contain no `session-manager.js`. Source/installed dependencies are not
pruned or edited. Pointer `/var/tmp/pr95-mcp-runtime-deps-stage`; this is build
feasibility evidence, not yet an integrated committed tree.

The required real positive is still pending: exact frozen `stack/bin/pi-native`,
local Pi installation of the copied PR77 package through `package.json`'s `pi`
manifest, disposable HOME/agent/project, `get_commands` and `/mcp-status`, no MCP
server approval, no child marker and no provider request. Extension installation
is distinct from saved project trust and exact MCP declaration approval. The
bundled CLI, `-e`-only fixture and acceptance helper's preapproved server are not
substitutes. `NODE_OPTIONS` network guards are stripped and cannot prove isolation;
use an external process-level network denial for this fixture.

The real canonical positive, real ancestor/Jiti negative integration, helper
wiring, changed package pins and independent import-closure review all remain
OPEN. Runtime discard/reopen, adaptive capture, ACP admission and keyed metadata
publication/recovery remain separate requirements.

## Independent writer review received during this work

`pr1-native-goal-preflight-independent-review` reports scoped CLEAN for the exact
`3585b0f` failed-manager correction only. Independent clean checkout/build:
`/dev/shm/ac-pr95-writer-review-own-mTjXZWo7/repo`; manager `10ac30c1…`, manifest
`1684f7d9…`, tree `7d16eb01…`. Own two-origin differential probe
`writer_delta_probe.mjs` SHA prefix `b32e791b…`; old/new logs `f6277edd…` /
`a4fe9ee5…`; independently rerun 3 × 38 = **114** owner continuations, log
`66810715…`. This is not an independent full-494 or wheel-51 execution claim.
Sink review and any combined clearance are not inferred from that scoped result.
