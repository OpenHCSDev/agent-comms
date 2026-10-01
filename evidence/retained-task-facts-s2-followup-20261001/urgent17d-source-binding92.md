Parent — URGENT17d deployment binding, independently rechecked read-only now. This takes priority over the474 combined artifact lane.

The discovered outer entry `/home/ts/.pi/agent/extensions/project-sync/index.ts` matches its declared85-byte SHA545decea80b2fb64d57f8f7ffd88b99496874b9a4bb08666ea852bdaae36c87e. The nested original `/home/ts/.agent-comms/extensions/pi-project-sync/index.ts` is still932 bytes/SHAa9519fa9c11dcc44eb34805e927b5c51478398cf64e8e3c8135fc41040ca6e21; the reviewed17d declaration requires1878 bytes/SHA1cceb41fe65f2d58d1f9838f9314e800906a0959173beee2a21892caf2e33d69. That exact mismatch explains the source guard refusal before provider input.

Reviewed source to bind: `/home/ts/wt/comms-native-tool-latency-allocation-20261001/extensions/pi-project-sync/index.ts`. This snapshot is also committed in merged Core1304d0657975020c148a83f40d3b1550d6e70355. `stack/native-import-manifest.json` declares both original paths, snapshot paths, sizes and SHA values. Existing `stack/native-import-fence.mjs` validates each original source before importing the already compiled ESM; package trust alone does not publish the original source.

Existing compiler command (ONLY a fresh private package with no existing generated outputs):

```sh
node /home/ts/wt/comms-native-tool-latency-allocation-20261001/stack/prepare-native-global-extensions.mjs NEW_PRIVATE_PACKAGE_DIR
```

Normal `stack/bin/prepare-pi-native` invokes this through `stack/prepare-native-import-boundary.py`, copying the declaration and compiling every reviewed snapshot with package-local esbuild. It does NOT deploy/write the original global TS. Its outputs use exclusive creation; never run it on the default17d donor.

For the existing reviewed17d artifact, no native rebuild is needed if your operator deployment safely preserves the shared-source preimage and publishes exactly the reviewed1878-byte/1cceb source. Outer entry already matches. If a different source is intended, freeze that reviewed snapshot/declaration and build a NEW package with new full commitments; do not alter17d, disable the guard, or omit the extension.

Default donor and independent stable17d copy remain untouched/protected. Earlier automatic-discovery01 already preserved the same refusal and zero-input evidence in `native485-artifact-ready87.json` / `native485-automatic-discovery01.log` on481. NotSent5f716b remains original, unreplayed. Parent owns global-source publication and actual affected entrypoint verification; Sch owns fresh compilation only if a new reviewed source binding is required.
