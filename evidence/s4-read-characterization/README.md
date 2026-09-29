# Original S4 Part B: current-contract acceptance

Production base: `420476a7`. Tests run against the noneditable installed core in
`.artifacts/paired-installed/lib/python3.14/site-packages/agent_comms`, with
`PYTHONPATH=tests` (not `src`). No production, Carver saved-transcript, Noether
caller-cleanup, live runtime, or launcher files changed.

## Requirement mapping

| Binding S4 requirement | Maintained evidence and remaining scope |
| --- | --- |
| Part B steps 1/6: any-mode expansion, DM delete/rebind, crash/reopen | Existing `test_read_ledger.py` and `test_view_unread.py` exercise these behaviors, including actual child process exit and family scope extension. Reused rather than duplicated. |
| Stable viewer/conversation identity; obsolete proof cannot ACK replacement | New human-incarnation replacement test: actual remove/register, old sparse read invalidated, old page rejected, fresh page ACK accepted. Ordinary re-registration preserves identity; initial test attempt correctly exposed that distinction and was replaced with actual deletion/recreation. |
| B3 / OPEN1: one durable authority with distinct transcript-read fact | New native-file/index recovery test: read only the captured transcript prefix; delete disposable SQLite index; rebuild from actual native entries; retain exactly one unpainted reply and unchanged canonical ledger. Bus page fetch does not ACK; bus ACK does not advance transcript offset; executor ACK affects neither human transcript offset nor bus facts. |
| ACP delivery cursor is not human-read authority | New combined test plus maintained `test_user_view_mark_read.py` and `test_view_unread.py`. Actual bus cursor operations, no substituted store or mocked delivery. |
| Steps 3/4: scalar/view2 compatibility and historical format equality | Superseded by round2 current-only format and owner's explicit instruction. No readers, migrations, aliases, or retired fixtures restored. |
| Acceptance 5 / OPEN8: mounted Toad painted read-ACK | Parent/Carver owns mounted UI path. `evidence/history-visible/README.md` proves retained transcript paint, not a comprehensive mounted ACK matrix. This PR does not claim that matrix. |
| Acceptance 6/7: certified NRA and full-suite scope | Not proved by these tests. No blanket original S4 completion claim. CI deferred. |

## Executed acceptance

```
PYTHONPATH=tests .artifacts/paired-installed/bin/python -m pytest -o addopts='' -q \
  tests/test_s4_read_characterization.py tests/test_read_ledger.py \
  tests/test_view_unread.py tests/test_thread_unread.py tests/test_user_view_mark_read.py
40 passed in 13.25s
```

Ruff check on the new module passed. The first invocation without overriding
repository addopts did not collect: this isolated environment lacks xdist/cov;
the executed command above explicitly disables those unrelated defaults.
No provider request, original-input replay, production format change, or
retired test mechanism was introduced. No redundant current tests were deleted:
they cover independent behavior, while obsolete fixtures were removed in prior
closure work.
