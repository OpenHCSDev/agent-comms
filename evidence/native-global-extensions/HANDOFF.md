# Native automatic global extension startup

## Delivered

Normal discovery now maps the four existing global extensions to native ESM
inside the committed deployment. Each mapping binds the exact original entry and
its full local source inventory. Changed/unlisted source is refused; no outside
module execution, Jiti fallback, package acquisition or `--no-extensions` bypass.
The existing import boundary and whole-package commitment remain authoritative.

Canonical authored owners are `extensions/pi-agent-comms`, `pi-project-sync`,
`pi-subagent` and `pi-web-search`. The newer global Comms model validation is
reconciled into the existing adapter. No `extensions/native-global` source copy
remains. Forwarding entry snapshots only record existing global entry bytes.

Manifest v2 replaces v1 directly. Native preparation derives the build catalog
from that manifest. Runtime derives its lookup from the same declarations.
`tests/test_native_package.py` now consumes `NativeOutcome.state` directly; no
mapping compatibility was restored. Backend diagnostics belong to parent's
ad46139 and are untouched here.

## Evidence

- `import-boundary.txt`: 14 local import-boundary cases passed, including exact
  source admission, transitive source mutation, unlisted identical source and
  dependency symlink rejection. This is module-level evidence.
- `actual-startup-final.txt`: exit 0 with ordinary automatic discovery, four
  extensions, all Comms tools, subagent/web_search and project-sync tool guard.
  The actual 143,224,441-byte saved session and its proof journal were copied into
  an isolated persistent fixture. Native `get_state` returned 714 messages and
  `pi-native-input-v1-live-only`. Kernel network denial; zero prompts/provider
  calls, no input replay. No private message bodies are in these receipts.
- `package-tests-before-fixture-migration.txt`: retained original 25-pass/3-fail
  result; all failures indexed the already-typed NativeOutcome as a dict.
- `package-tests.txt`: corrected current caller, **28 passed**, exit 0.
- `canonical-build.txt`: rebuilt all four compiled modules from canonical source
  directories; identical to the modules used in the actual startup receipt.
- `package-verification.txt`: final complete package commitment verified.

Actual startup command (exit 0, before canonical source relocation):

```sh
PYTHONPATH=src /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python \
  stack/test-native-global-extensions.py .artifacts/package .artifacts/startup-final \
  --session /home/ts/wt/comms-acp-saved-session-startup-20260928/.artifacts/acp-reopen/session.jsonl
```

Canonical relocation changed manifest source references only; compiled module
identity was checked afterward and the final manifest/package commitment updated.

## Parent integration and remaining acceptance

Prepared independent native package (194 MB):
`/home/ts/wt/comms-native-global-extensions-20260928/.artifacts/package`

Final tree commitment:
`ae844ae9c0fdb5efbf8782818e781b0d81f5911d5488e308f3911abc77ff932e`

Integrate this branch with ad46139; publish this package into a new immutable
deployment location, with this branch's `stack/pi-native.sha256`. Do not apply
native preparation again to the already prepared package. Parent owns wrapper /
installed startup verification and normal activation. Source acceptance here does
not claim that the live package or owners have been updated.

The manifest intentionally binds this installation's exact source paths. New or
changed extensions require a new declared build, not dynamic installation.
Subagent/web-search registration is verified; their provider-executing operations
were not invoked. Original global files, live session, installed package and
backend.py were not modified. Only owned fixture copies were used.

Rollback before activation is discarding the candidate; after activation parent
can restore the previous package/launcher. No saved-data format migration occurs.
Copied session fixtures are disposable; receipts and reproducible harness stay.
