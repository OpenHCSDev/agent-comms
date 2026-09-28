# Round-two implementation assignments

Updated 2026-09-28. Source: the owner's `plans/refactor2/` package, published here
so all worktrees share the same rules. This adds to the active deletion goal;
original C0/S/R/D/PF closure is still required. Superseded archival-converter
proposals in the first deletion ledger are replaced by round-two hard cutover.

| Surface | Implementation owner | Branch / dependency | Draft PR |
| --- | --- | --- | --- |
| S13 / A12 | Lovelace (01a0e872-6948-7ef0-b3ab-9aecf1ef3849) | refactor/round2-s13-child-process; new owner module first, then backend/owner lifecycle/recovery | [Comms232](https://github.com/OpenHCSDev/agent-comms/pull/232), draft; real process tests pass, remaining callers in progress |
| S12 / A13 | Cicero (01a0e872-6998-7790-8839-227803a44a1f) | refactor/round2-s12-typed-tables; new table owner first; wait for Pascal before touching his native evidence callers | [Comms230](https://github.com/OpenHCSDev/agent-comms/pull/230), draft; full caller closure pending |
| R0 | Copernicus (01a0e872-69dc-7143-8692-7950ea46f2bd) | refactor/round2-r0-debt-ratchet; tools/workflow/script tests only | [Comms228](https://github.com/OpenHCSDev/agent-comms/pull/228), merged; required check active and skipped-CI refusal verified |
| L0A channel/catalog/tools | Darwin (01a0e526-04a9-7981-bbb1-f152b3a9edf5) | Completed source B2 branch; parent owns one-shot activation/deletion | Comms231 and Toad106 merged; quiet installation pending |
| PF3/response authority/typed claims | Pascal (01a0e525-90a4-77e2-80df-703f622e20f5) | Existing B3/B4 branch; current assignments finish before S12 caller adoption | [Comms226](https://github.com/OpenHCSDev/agent-comms/pull/226), merged; quiet-step installed acceptance pending |
| L0B and remaining L0A closure | Parent | refactor/canonical-bus-retirement-20260928; includes source audit, D22 tool, read API cleanup; no duplicate L0B agent | [Comms229](https://github.com/OpenHCSDev/agent-comms/pull/229), draft; canonical writer/drain and sole read-ledger implemented; 35 integrated channel/read/idle checks pass; D22/reset admission closure open |
| L0A core loaders | Copernicus (01a0e872-69dc-7143-8692-7950ea46f2bd) | refactor/round2-l0a-loaders; goals, registry and thread management; S13 owns threads.py | [Comms235](https://github.com/OpenHCSDev/agent-comms/pull/235), draft |
| R1 | Nietzsche (01a0e874-0617-7a91-a702-0bc90325b53d) | NRA ~/wt/nra-refactor2-r1-20260928; existing PR8 is dispatch-lead work, current wire NRA owners stopped; inspect claims before edits | Pending actual changes |
| S10 | Pascal (01a0e525-90a4-77e2-80df-703f622e20f5), next task dispatched after226 merge | V1/V3 first; V2 requires A13, child adoption requires A12 | [Comms234](https://github.com/OpenHCSDev/agent-comms/pull/234), draft; A12 adoption resumed |
| S9 / A14 | Darwin (01a0e526-04a9-7981-bbb1-f152b3a9edf5) | ~/wt/comms-refactor2-s9-20260928; K1/K2/K5/K6 and A12/A13 adoption | Code-bearing PR pending |

PR pending is not PR open. Workers publish code-bearing drafts promptly and keep
the full surface incomplete until guards/callers/deletions and acceptance finish.
No agent opens an empty placeholder or duplicates another owner's implementation.

## Integration and cutover

- Parent integrates and installs; it is also implementing L0, not a separate
  coordinator agent. Existing subagents continue useful independent coding.
- Persistent worktrees only `~/wt`. Shared dirty main and user history are untouched.
- D22 is approved in `docs/DECISIONS.md`: preserve history in place by one rewrite.
- Each surface declares exact runtime stores to reset and durable stores to retain.
  Never bulk-delete the live root or infer permission to replay uncertain inputs.
- Run local surface guards, relevant external-contract tests and actual affected
  paths; full local suite on the integrated tree. Slow CI remains asynchronous.
  R0 implements the new fast required check specified by D18; no exception list.
- Quiet install once per completed step: no active turns/compactions; pinned core
  and paired Toad together, applicable one-shot tools, declared resets, owner
  relaunch. Delete executed tools before marking their surfaces complete.
- Record added/deleted source and tests per PR. Keep failed evidence honest and
  remove owned disposable fixtures after processes have exited.
