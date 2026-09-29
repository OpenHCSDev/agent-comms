# 318 correction using the Sep28 22:16 audit skill

Reread actual /home/ts/.local/share/agent-comms/skills/refactor-audit-20260928-2216/refactor-audit: SKILL.md, patterns/README.md, IMPL-14 last section and scripts/audit/chain_terms.py. Both requested skill links resolve there. NRA skill also reread.

## Classification and ownership

- IDEN-3 in SelectedOriginalBinding.bind: session_file absence and unreadable revision were two probes for the same source state. Existing backend._session_revision already accepts an absent path and returns None for absent/unreadable sessions. Deleted the caller's duplicate path check. Exact key cardinality, already-bound refusal and revision proof remain; no condition was weakened, nested or replaced by one-rule-per-term classes.
- IDEN-3 / IMPL-4 / IMPL-5 in RoutedOriginalInput.allows_goal_input: dependency input and ordinary routed input had different active-goal authority but one optional-field predicate. DependencyOriginalInput now owns the dependency case, selected at the existing OwnedTurn source construction boundary. Ordinary original/followup inputs share RoutedInput and existing InactiveGoalPermission. Exact dependency wait ID, sender incarnation and after-sequence checks still run through existing authorities before any write. Deleted the repeated None/goal-state tests and duplicate routed followup implementations.
- TIME-9: no stored format, codec, adapter or converter change. No live changes.
- AGENT-8: used the actual updated declaration-owned census, not a new script with copied measure rosters.

## Executed evidence

Installed wheel of corrected318 in owned .venv. Native package5fde; loopback provider, no paid calls.

- focused.log:30 passed,1 actual-native case deselected,15.47s. ACP original/goal admission and selected handoff revocation/binding cases.
- native.log: actual ordinary summary handoff passed; new dependency fixture failed because its Message omitted required type. Retained red:1failed/1passed18.40s. Product source unchanged after this run.
- dependency.log: corrected fixture1passed0.66s; exact dependency passes, ordinary active-goal input refuses, missing/replaced waits refuse, pre-wait message refuses with exact existing rule reasons.
- guard.log:4 passed; newly added source/binding long-chain guard plus existing owner/deletion guards.
- census.json: against original PR318 basea1d24d3a, no touched file increases boolean_chain_terms. New input source/binding files both0; OwnedTurn decreases48 terms. Other new modules0.
- ratchet.json: no positive existing measure delta againsta1d24d3a.

Same native True/None/False callback and lock-through-write unchanged. Parent merge/install remains;323 orchestration work is separate.
