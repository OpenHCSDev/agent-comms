# Explicit endpoint context-budget rejection recovery

Owner: Mendel. New scope distinct from merged #450 tooling and #452 BUS lifetime.

User original openhcs-helper2 `test`, ACP request7 in Agent_Comms_2026-09-30T11_28_15_909242.txt, failed400 with endpoint limit1048576; requested1076118 = text427363 + tool5273 + output643482. Preserve this started input and failure, never replay it.

#406 merged fc47c15a214bdc0808f759043579b323909a8c84. Both installed nativee36 and reviewed ce ca source contain its ContextBudgetRequest import and identical policy SHAe3572ebbb69891ae0789c90aa018eef981e335a4c1113ff90ec10f53740b0a10 / providerSHAada3b43980c4fd627cf67c8c7836c0b8118e4fb3b0e7bf49adf3c7b7192c9383. Existing decoder recognizes only the prior input-messages/completion grammar, not the observed endpoint text/tool/output grammar. This is a missing external-boundary case, not evidence that406was absent.

Scope: existing stack/native-context-budget.mjs centralized typed ProviderRejection normalization and capability policy; affected declaration-owned request integration and original installed native fixture. No scattered regex, per-call output clamp, fixed reserve/cushion, artificial compaction threshold, alternate parser/store or legacy reader. Extend boundary grammar through its behavior-owning declaration; semantic rejection counts authorize a strictly decreasing allowance only for the same rejected pre-stream request.

Acceptance: existing actual native/ACP/private owner/local provider continuous explicit400 -> same request context with revised allowance -> accepted stream; original native input once/no compaction; unrelated400 unchanged, count/allowance disagreement refused, acceptedstream failure never retried. No paid provider or public mutation required. Reuse406 journey and installed trust recipe with fresh owned persistent scratch/native artifact; do not alter reviewed e36/ceca or #450 stages. Parent owns activation.

Packaging coordinated directly with Schrodinger; next native budget artifact ownership Mendel after source checkpoint, immutable ce ca stage can ship independently with the known budget gap recorded. Arendt #452 admission/source custody excluded.
