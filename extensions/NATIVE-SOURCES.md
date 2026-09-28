# Automatic global extensions in the native deployment

`stack/native-import-manifest.json` declares each native ESM entry and its exact
external source inputs. `snapshot` points to the canonical repository source;
there is no separate authored native adapter. The Comms adapter includes the
currently installed global model/thinking-level validation. Project-sync,
web-search and subagent retain the actual installed implementation.

`global-entry.ts` files preserve the existing global forwarding entry bytes for
source verification. Their targets are built from the canonical directory's
`index.ts`; they contain no second implementation. The build resolver uses the
manifest's original paths to resolve these imports without evaluating the live
files. All local inputs must be declared and used. Host SDK peers remain external
and resolve inside the committed native package.

The source paths in this deployment manifest are specific to this installation.
Normal Pi automatic discovery selects those paths, verifies their complete source
inventory, and imports the committed ESM result. Unlisted paths or changed input
bytes fail with an instruction to prepare a new deployment. Runtime TypeScript
evaluation, package acquisition, and outside-package imports remain disabled.

To update an extension, edit its canonical source, reconcile the intended installed
source inventory, and update that declaration's paths, byte lengths and SHA256
values. Run the existing native preparation in a new package directory; it calls
`prepare-native-global-extensions.mjs`. Publish the resulting whole-package
commitment with the source change, verify automatic discovery, then activate that
new package through the normal deployment owner. Never edit a running package.
Global source activation must match the declared source bytes; it is not performed
by the builder or these tests.
