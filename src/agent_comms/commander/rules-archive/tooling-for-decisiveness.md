---
name: tooling-for-decisiveness
description: Lean on tools (interpreter, NRA, coverage, tracing) to settle refactoring decisions with facts instead of reading and arguing
type: feedback
---
Lean into tooling for decisiveness (owner, 2026-10-12): "AST scan is not bad, but we should lean into tooling." Also: "you're really good at working out the relation; do you ever consider using the Python interpreter?" I had been reconstructing class relations by reading text.

**Why:** the interpreter and the owner's own tools already know answers that text only hints at; a tool is not talked into things by surrounding code, and it costs far less context than reading.

**How to apply:** map each hesitation to a tool, and have agents run them before designing and cite the output in receipts:
- who supplies what / is there an existing owner -> `.claude/verify/supply_map.py` (live import: MRO owners, provider crossings, identical-bytecode copies, forwarders, registry contents and overwrites, structural twins, glued names; `--focus`).
- really unused? -> a reference prover (static refs + import-all + string/reflection uses across code, tests, docs, saved pipelines) issuing the "unused" certificate.
- what depends on this -> coverage with per-test contexts.
- are these the same -> bytecode equality (exact) or run both on generated inputs (test-grade, say so).
- which route actually runs -> trace a smoke/parity run recording which declaration answered.
- new case needs no edit -> a generic throwaway-member harness.
- Use the owner's NominalRefactorAdvisor for full findings on an area, not only its r1 ratchet as a gate.
Tools give candidates and facts about supply and readers; whether a behaviour should exist is still the owner's (science) and the papers' (form). Build tools under `.claude/verify/` without worktrees. See [[optimize-for-slop-elimination]], [[glued-class-names]].
