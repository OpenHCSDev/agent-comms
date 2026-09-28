# Current native deployment package

Source: parent `c3e7252a` (including PR257), pin/test commit `d2ce1ccd`.
PR: https://github.com/OpenHCSDev/agent-comms/pull/258

## Ready package

`/home/ts/.local/share/agent-comms/native-current-689ce4b5d0592b9a/node_modules/@earendil-works/pi-coding-agent`

Manifest SHA256: `689ce4b5d0592b9aa6b6701cec0a0df6407c091cc33af5091d083ef15f16d908`

Complete native tree commitment: `14e9dcc0ff9fd4d7952f62c91e554d0781c54deb4a9ae938721f431bd4e37fa4`

This is a new complete immutable-by-policy package directory, never repaired in place.
Parent must take the new manifest pin with the package. No routing/launcher/owner
state changed here. Existing installed0d7 package is preserved while parent consumers
still use it. Do not describe PR257 as live before parent activation.

## Build and verification

The current source differs from the previously accepted native production inputs in
exactly MCP README, MCP inventory implementation, and selected_claimed_write.mjs.
A disposable reflink copy of old0d7 with these three current authored files supplied
expected pin calculation, then **unmodified canonical stack/bin/prepare-pi-native**
rebuilt independently from pinned stock through every current patch/extension
packager and verified that exact expected manifest/tree. The bootstrap copy was
never launched and has been deleted. Final package was copied to the stable new
digest directory and all individual hashes plus whole-tree verifier passed again.

`prepare.log`: canonical rebuild exit0.
`installed-receipt.json`, `installed-final.log`: fresh installed actual CLI identity,
get_messages, native proof capability, exact preserved saved fixture, zero provider
requests, retired child, current packaged inventory v2 and absence of compatibility.
Reproduce: `python3 evidence/native-current-inventory/verify-installed.py`.
The script creates/removes its own persistent fixture, with a declared local-only
model whose port is closed; it sends no prompts or network requests.

`claim-loader-final.log`:2 passed2.41s, real native loader accepts packaged selected
tool and refuses outside-package copy.
`actual-selected-claim.log`:1 passed3.64s, actual SelectedExecution using installed
native CLI and deterministic localhost SSE, packaged selected_claimed_write,
authenticated owner socket/current typed envelope, real file mutation, exact reply
publication, completed/released execution and refusal to execute it twice. No paid
or external provider. Uses the existing selected-execution fixture with a new
selected-write parameter; normal four-tool cases remain intact.

Commands (xdist absent in interpreter; defaults cleared):
```
PYTHONPATH=src PI_COMPACTION_TEST_PACKAGE=/home/ts/.local/share/agent-comms/native-current-689ce4b5d0592b9a/node_modules/@earendil-works/pi-coding-agent timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -o addopts='' -q tests/test_selected_tool_native_package.py
PYTHONPATH=src AC_NATIVE_COPIED_PACKAGE=/home/ts/.local/share/agent-comms/native-current-689ce4b5d0592b9a/node_modules/@earendil-works/pi-coding-agent timeout 60 /home/ts/wt/comms-historical-views-20260927/.test-venv/bin/python -m pytest -o addopts='' -q 'tests/test_selected_execution_native.py::test_native_full_four_tools_publish_and_release[False-True]'
```

## Honest limitations / previous attempts

`claim-loader.log`: initial launcher used unavailable xdist -n0; no cases ran.
`installed-first.log`/`installed-second.log`: own fixture had non-owner-only mode;
native correctly refused. `installed-model-fixture.log`: missing fixture model
context; corrected declaration. `installed-new-session-initialization.log`: fresh
header-only fixture legitimately appended initial model/thinking; use existing
saved model/thinking rows for read-only acceptance. No production guard weakened.
Final installed probe passes exit0 with empty stderr.

No repeat600MB tests, broad suites, provider acceptance or live activation here.
Unchanged EntryStore/proof startup capacity remains backed by Lovelace PR232's
current-helper302/604MB receipts; those alone do not establish arbitrary future
proof-journal growth. Earlier requested broader growth review is not completed by
this urgent package rebuild. T3 remains deferred per parent priority.

## Coordination / cleanup

Lovelace PR232 informed old0d7 receipts stay valid for unchanged EntryStore, not
PR257 packaging. Nietzsche PR255 informed current producer envelope is included;
no duplicate source fix requested. Small failed receipts retained; own unlaunched
pin-bootstrap removed. Stable candidate and canonical builder output retained for
parent adoption. Existing old0d7 copies retained because consumers may still use them.
