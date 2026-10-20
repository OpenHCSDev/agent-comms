# P241: shared commander load and growth

Owner: pi-cn-phase2. PR #1965. Rebased onto requested main `15ba6edce`.

## Ownership

- `commander/capacity.py:Load` owns native and CLI load, the weighting formula,
  and selection of receiving child commanders with retained orders. The CLI
  uses its supplied commander context; it does not construct a competing one.
- `turn_digests.py:OrderRead.for_commander` owns the shared order/holder selection
  and incarnation check. Both full edit reads and count-only reads consume it.
  `OrderEditCountRead.count` serves load and the unjudged standing rule. The
  competing `CommsUnjudgedTool.readers/count` methods are deleted.
- The heartbeat reader/writer, its CLI write and its config-directory creation
  are deleted. Registry parentage/status and held orders supply that fact.
- `commander/standing.py:MergeGrowth` reads one captured first-parent Git history
  with numstat, not a fetch followed by two subprocesses per revision.
  `repo.py:Repo.git` owns bounded checked execution and remote discovery.
- Main's `CommanderContext`, argument parser, order lifecycle and recorded-check
  progress implementations are preserved. `ToolCaller.acting_name` selects the
  explicitly bound caller or the context's configured commander. The CLI uses
  the existing concrete caller states and FieldCodec normalization, not a nullable
  name or a second context. Checked order reads obtain their cutoff from the order.

Before-rebase AST evidence uses the existing audit Package/Repository parser
across Core, tools and tests: `p241-2200-ast-before.txt` in this unit's scratch.
Receiver bindings are syntactic; dynamic resolution and state freshness are not
proved by the census. The previous retained source evidence is also preserved.

## Verification and limitations

The 20:52 checkpoint was broken: a CLI FieldCodec reference lacked its import.
Its retained tests reported 11 passed, one failure and 36 errors. Both ratchets
exited 2 because the command omitted the required `--head`; those were not
successful checks. The current rebase removes that obsolete CLI identity change.

Current checks use the shared interpreter, background `timeout 300` commands,
and the unchanged checker against the requested main. Results and application
receipts are retained under `/home/ts/.cache/agent-scratch/pi-cn-phase2/` with
prefix `p241-2200-`. No installation or provider input is performed here.

Final source checkpoint `d00ab51d7`: **49 tests passed** under background
`timeout 300`; source ratchet **1 solely SourceLines +8**, with no introduced
findings; tools ratchet **0**. Both ran separately against `15ba6edce`, with
`--head` supplied. Before/after AST acquisition reported no parse omissions.

Actual configured saved-state native handler read **32 open / 30 overdue /
1,860 unjudged / total 278.0**. The real CLI then read **32 / 30 / 1,861**;
the count increased by one between reads, so this is not a frozen parity sample. The
focused test checks exact native/CLI equality on one retained fixture. This is
not an installed native-transport or provider-process verification.

The 22:10 readiness deadline was missed: the final fork argument representation
needed correction. No failed or running check was represented as successful.

This delivers the shared load/growth checkpoint. Broader timing/alert parity and
P244's comparisons are not claimed complete. Earlier native/CLI samples are
historical evidence only, not proof of the rebased artifact. No blocked goal is
reset, and no uncertain operation is replayed.
