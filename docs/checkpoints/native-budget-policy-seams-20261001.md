# Native budget policy seams for Core485

Read-only inspection of the actual ActiveRoute native package on2026-10-01:

`/home/ts/wt/comms-retained-task-facts-s2-20261001/stack/.pi-native-0064a96bb79c21c1/node_modules/@earendil-works/pi-coding-agent`

No public input, native journal, installed package, route or owner was changed. The reported254272 native usage and Started failure are parent evidence; this inspection does not independently allocate that particular failed request.

## Existing declaration owners and divergence

| Consumer | Original decision/source | Authoritative source location |
| --- | --- | --- |
| Cold restored history | `SessionContext.restore`: current selected usage or history message estimate against context window minus reserve | `stack/native-session-context.mjs` -> `dist/core/session-context.js` |
| Queued selected preflight | `acSelectedCompactionSettings`: stored-context requirement or `getContextUsage()` passed to `shouldCompact` | `stack/native-compaction-selected-summary.mjs` -> `dist/modes/rpc/rpc-mode.js` |
| Python selected compaction | `maybe_compact_owner_turn` consumes that decision before native send; its pending `input_text` is used after the trigger, not by the decision query | `src/agent_comms/owner_compaction_adaptive.py`, `selected_pi_route.py` |
| Native threshold | `shouldCompact`: context tokens greater than context window minus settings reserve | stock `dist/core/compaction/compaction.js`; native hooks in `patch-native-auto-compaction.py` |
| Final provider admission | `ContextBudget`: actual API semantic input after hooks; estimate is `max(native-prefix estimate, final serialized-input heuristic)` | `stack/native-context-budget.mjs`, `native-generation-policy.patch`, `native-request-input.mjs` |

This is separate decision ownership/input coverage, not proof that measured usage and an estimate are illegitimate duplicates. The measured prefix, appended input, transformed API input and generation reservation are distinct evidence. They need one admission relation and a declared coverage relation; none can be treated as an interchangeable total.

In frozen0064 `ContextBudget.allowance` checks available context before handling an absent desired output allowance. An absent explicit API output parameter therefore still refuses when final estimated input fills the model context. Omitting `max_tokens` cannot repair an oversized-input admission. An explicit output capability is also distinct from generation intent/reservation; using the catalog's maximum as a forced requested completion would reintroduce the earlier failure.

Managed native context intentionally bypasses unjournaled `_checkCompaction`/native tracked overflow retries when `AGENT_COMMS_NATIVE_CONFIG_DIR` is set. Preserve this journaled owner fence. A final admission failure must not become a string-matched replay path or an unjournaled second compaction.

## Guidance for Arendt's one complete mechanism

Extend the existing request-local `ContextBudget`/estimate owner rather than placing another threshold in Python, `ContextSegment`, a UI status, or a cached usage field. Its relation should expose total prospective input evidence, selected model capability and generation reservation to both selected compaction preflight and final admission. The effective selected settings/model must remain with the retained native session's existing owners.

The selected preflight must account for the original pending rendered input and applicable system/tool/image/transport transformation overhead. Its current query sees selected historical usage, while final API admission sees the transformed prospective request. Join those through existing original input/context/model witnesses; a number copied from current usage alone cannot certify the final request. Post-hook final admission must validate the same relation against that actual request, not trust a stale preflight measurement.

Keep the estimator's validity rules: pre-compaction assistant usage cannot certify a new compacted prefix; retained current usage and a heuristic may disagree without either being an authoritative tokenizer. Record the measured prefix/trailing estimate/final transformed estimate and their coverage when allocating the original failure. Do not sum overlapping totals, erase the final estimate, infer a hardcoded model cap, or merely lower a trigger until this case passes.

If the prospective relation requires compaction, use the existing selected operation/journal before binding the original native input. If mandatory original input cannot fit even after compaction, the existing typed admission failure must describe that original refusal. This guidance grants no replay of already Started public inputs.

## Frozen source proof

Actual package file SHA256s:

- `node_modules/@earendil-works/pi-ai/dist/api/agent-comms-context-budget.js`: `f9224adad46d6bc83e7ebe6fe1daeb8b1f79dde278dab11ee705574ff10e8c45`
- `node_modules/@earendil-works/pi-ai/dist/utils/estimate.js`: `d2cdfd5d0daf4c0df0f52af14773b338045d0c67ae7b564b681c34d3fab013f1`
- `dist/core/session-context.js`: `e9762b81b6dcb400cb761ddcfdb48293ae277bd9ae836359e2a0b1facb84500a`
- `dist/core/agent-session.js`: `709bcbf952a648587e3f1961b79cf1e7a95095758018bd1f956b416784bfa06f`
- `dist/core/compaction/compaction.js`: `bfb76a0e347c27dd0d09e87b15d2b6e6d6ac7c4f9a9eb7073d05bf8dee048686`
- `dist/modes/rpc/rpc-mode.js`: `2077e5a2c54b072fa46a124ebe469bd478ee885abae83a7315f5c3c867260c50`

These prove inspected source, not model metadata, failed-request payload values, a fixed public defect or installed candidate acceptance.

## Native builder and S2 boundary

Arendt485 owns budget policy and its producer/consumer closure. Core481 remains original USER/input/fact provenance; its only change in `turn_context.py` is `Provenance.require_human_input`, with no changed `ContextSegment`, estimator or budget policy. Retained exact facts are a real input contribution, never truncatable pin wording or a second budget authority. Einstein474's frozen wire USER79 checkpoint and the accepted three-compaction direct-input proof remain unchanged.

Once485 publishes reviewed source, the existing ordinary stock builder `stack/bin/prepare-pi-native` installs `native-context-budget.mjs` and `native-request-input.mjs`, applies `native-generation-policy.patch` and selected-session patches, then verifies the manifest/module/import/fulltree contracts. Generate new pins in that existing lane; build a fresh package after headroom permits. Do not modify0064, Core481's borrowed776 or public originals in place, create another bundle concurrently, or change a public runtime before the operator's reviewed paired cutover. No native build is started by this helper.
