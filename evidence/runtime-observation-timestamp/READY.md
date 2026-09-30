# Required runtime observation date: source checkpoint Ready

Production d44c3f79; final test/source head80a3f93b. Delete eight production lines,
add seven across runtime_info.py, agent_activity.py and TurnRunner's existing
prepare_selected_session return. Required timestamp is owned by the record;
only the two actual producers capture time. The missing-date decoder and
default-now are deleted. No alternate reader, new store or codec subclass.

All direct production and fixture constructors are migrated. Dated serialization,
rename, cold reopen, lock exclusion and failed-publication rollback remain.
Missing/malformed timestamps fail through existing strict FieldCodec without
reading a clock or rewriting the file. Nineteen focused controls passed in0.10s.
Existing ratchet snapshots for three claimed production files have zero delta.

Actual installed-wheel/native gate passed in6.27s. It uses the existing
native_backend and canonical_agent fixture, normal prepared native593, real
NativeSessionPreparation and TurnRunner.prepare_selected_session, then actual
AgentActivity observation, storage, clock-disabled reopen, rename and reopen.
Zero native input and zero provider posts; original retained native bytes are
unchanged. The exact owned child is alive during preparation and gone after
existing shutdown custody. Full installed Python source hashes match the tree.

Two failed fixture runs are preserved: missing explicit native model selection,
then a never-opened journal's legitimate configuration append. The final fixture
prepares configuration through native's own API before checking exact retained
bytes; no assertion is relaxed or original input replayed. No public/native
bundle mutation or paid call occurred.

Activation dependency: **runtime_info.json is runtime-only and must reset at
the next declared quiet Core cutover before the target reads/launches**. The
existing parent/Arendt operator owns that boundary; use the existing strict
store's complete replacement under that custody. This source includes no new
operator. Preserve original evidence and all durable wire, native journal,
registry/goal/input history and UNKNOWN dispositions. No runtime legacy reader.

Owned persistent scratch:
/home/ts/.cache/agent-scratch/comms-runtime-observation-timestamp-20260930.
Raw native03 logs, installed hash/direct_url receipt, native journal and observed
runtime file are retained; the two failed runs are also retained. No process
remains owned by the successful test. Wheel/installed stage are disposable after
review and are not borrowed Core/Text/native dependencies. This follow-up does
not alter the frozen457 input/Todo checkpoint or Kepler251 UI acceptance.
