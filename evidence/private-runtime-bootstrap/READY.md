# Private runtime bootstrap ready

Base: current main b64014d2. Own tree: ~/wt/comms-private-runtime-bootstrap-20260928.
PR278: fix/private-runtime-bootstrap-20260928. Parent owns live repair and message151.

## Code

The existing TypedTable registry now discovers a PrivateRuntimeSchema capability
implemented by the canonical cohort, response, native-runtime metadata declarations
and PromptBinding. Those hooks invoke the existing exact-schema installers. The
MutationStore bootstrap loads these declarations and invokes their derived family;
it does not keep a separate function/table list or write a second schema.

The canonical Publisher fresh-protocol issuer bootstraps before exposing the private
marker, covering both Messaging.initialize_private_initial_protocol and automatic
ordinary first-send initialization. Explicit private owner launch bootstraps before
participant registration/spawn. The foreground launcher now uses the same method;
its old four-installer roster is deleted. Readers and inbox drain do not install.
Existing mismatched schemas still refuse launch; no conversion or repair added.

Manual schema setup removed from ACP fixtures and the fixture used by the existing
actual selected-native coding test. Production changes add 57 lines/delete17:
this adds the previously missing lifecycle connection plus nominal installation hooks.
Tests add201/delete16, including real detached owner send/restart and drift checks.

## Receipts

- native-current.log: 3 passed in24.79s. Real pinned Pi, loopback-only model,
  ordinary peer send -> actual detached worker/ACP wake -> durable reply -> stop/start
  -> second peer message/reply. Both fresh automatic protocol and a disposable
  base-only coordinator pass, with distinct native inputs and durable native entries.
  Third case verifies a drifted existing cohort schema refuses private owner start
  and is not repaired by ordinary readers or MutationStore construction.
- installed.log: 3 passed,3 deselected in15.74s. Wheel import confirmed from this
  tree's site-packages. Fresh detached peer/restart, drift refusal, and existing
  test_native_full_four_tools_publish_and_release[False-False] (real Pi read/edit/
  write/bash and response receipt) pass without fixture schema installation.
- foreground.log: focused source check of the final foreground caller migration.
- focused-fixed.log and focused-current.log: 39 passed,8 ACP failures.
- baseline-acp.log: unchanged b64014d2 independently reproduces those same8 failures
  (20 passed), involving cursor/fake-model assertions and changed error text.
  No claim that the ACP file is fully green; no new bootstrap failure diagnosed.
- native.log: initial run reached actual native reply in both cases but the test
  incorrectly looked for a registry session_file. Corrected to inspect the actual
  native-runtime input's persisted session_file; delivery assertions retained.
- focused.log: initial collection error from deleting a now-empty fixture with
  block, corrected before execution. Retained honestly.

Pinned native package: ~/.local/share/agent-comms/native-current-5fdef596596173bd/
node_modules/@earendil-works/pi-coding-agent, verified against this main's manifest.
No paid provider call, live state edit, native proof change, provider diagnostic
change or T4 work. Disposable native roots/owners clean up in fixture finally;
removed owned baseline source copy after comparison. CI deferred.
