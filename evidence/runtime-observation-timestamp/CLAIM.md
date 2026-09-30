# Required runtime observation timestamp

Receiving owner: Mendel. Assigned by parent after the useful Core457 input/Todo checkpoint; baseline17a43e02. Core458 integrates the frozen input checkpoint independently. Arendt456 retains native admission/proof custody.

Claim runtime_info.py and AgentActivity.set_agent_info; in TurnRunner only the AgentRuntimeInfo return in prepare_selected_session. All other native/session/admission methods remain Arendt-owned. Update every direct fixture constructor for an explicitly recorded observation, and delete old-undated acceptance from runtime_collaboration_documents.

Required relation: the producer records the observation time. AgentRuntimeInfo requires that original timestamp. Decoding, rename, reopen and publication must preserve it and must never invent freshness. Missing/malformed dates are rejected by the existing strict FieldCodec. No decoder override, default-now-on-read, old shape reader, field alias, second observation/store or schema adapter.

Storage: runtime_info.json is exclusively runtime observation state. It must be reset through existing quiet-cutover custody before this source reads an old undated file. Native journals, original durable wire/registry/goal/input/UNKNOWN records and historical proof receipts are protected. This source contains no cutover operator/public mutation.

Patterns TIME-1/3, IDEN-1/3, BOUND-1/2; library JSON/clock representations are external boundaries, but optional freshness is our declaration. Preserve exact dated serialization, rename, cold reopen, lock exclusion and failed-publication rollback. Use existing actual installed store and producer paths; no provider or native prompt replay. Kepler251 critical UI acceptance proceeds without this follow-up.
