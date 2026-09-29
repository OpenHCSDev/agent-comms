# Transcript source owner gate closure

Follow-up to merged #420 and paired Toad #207. No wire field, codec shape, source frontier or input-admission change. Existing native-only cursor capability stays distinct from an assigned source frontier.

The packaged required ratchet identified five new foreign absence probes. Cursor receipt inclusion/source validation now belongs to TranscriptCursor; AssignedSourceIdentity compares root and ThreadIncarnation as one value; NativeRecord owns decoded projection and incomplete-tail reporting. NativeEntry guarantees one recorded clock for all its projected parts, so the consumer reads that clock directly. No guard exception, allowlist or ratchet change.

Core packaged ratchet: exit 0 against original #420 base; no positive deltas. Toad packaged ratchet: exit 0, removing two long chains/eight terms/two foreign probes. Original exact installed physical acceptance remains applicable: 41.6 MB native saved source, 31-second continuous open view, ACP reconnect, first prompt, five physical A/B/A immediate sends, fresh receipt/handling once, 11 expected localhost provider calls and no old replay. All changed owner methods preserve the same data and operations; focused source/native-record checks rerun after normal main integration.

Exact original installed receipt: /home/ts/.cache/agent-scratch/toad-restored-inbound-chronology-20260929/candidate-switch-final.txt; manifest and retained source/profile/logs in sibling candidate-switch-final directory. Parent owns final paired activation and fresh default entrypoint check. No live roots, owners, user originals or uncertain inputs were changed.
