# Selected execution owns its acquired native launch

Base09b5086c, parent #587 merged. Mendel owns selected execution, stage/admission,
tracked launch and their existing NativePiRpcLaunch factories. Arendt confirmed
these claims disjoint from #589 read-only EntryStore/SessionContext/TurnContext.
No native edits/build, provider request, new environment or worktree.

Current source: SelectedExecution.validate verifies the package before any claim.
Each selected TRIAGE and FULL then calls TrackedTurnSession.execute, whose tracked
launch acquires the same immutable tree anew. The first child is an independent
fresh launch and still verifies; a subsequent stage in this same one-use execution
can derive its launch from that execution's actual first NativePiRpcLaunch.
Existing retained_managed already derives from an acquired launch. Extend the
same owner for tracked launches, keep independent fresh acquisition unchanged.
Do not borrow header, model, auth, input, lease, revision, child or readiness.
Per-input selection and runtime attestation remain their original facts.

Existing SelectedExecution owns the actual acquired launch resource for this run,
not a verified boolean/path cache/global trust store. The next stage assembles its
complete new launch through original session/configuration behavior. Different
packages must acquire afresh. Source coverage/UNKNOWN/NotSent are unchanged.
Pattern IMPL-12 repeated procedure/resource acquisition; TIME-7 no policy copies.

Before AST parses726 src/tests/tools modules, zero omissions,198 related sites.
Syntax is not dynamic-resolution proof; complete selected request/launch/custody
implementations are read. Managed warm-key reuse and original immutable-package
policy are distinct from a same-UID sandbox. Tests and installed affected controls
come after coherent source. Original13.562/98.141 negatives and protected581 source
remain; no latency claim from the old3.938s one-verify profile.
