# Native proof journal decoder closure

Based on current main46b729cf. Wegener owns native reader cap/run_native_pi_turn;
this change touches only proof declarations and retained journal decode.

Native `.input-proof` is durable native evidence paired with native session history,
not a rebuildable runtime index. Preserve it and session files in place. No schema,
producer, format, stored key/value or installed reset changes; no one-shot conversion.
Current schema1/type=context_committed is declared on NativeContextJournal and
strictly decoded by existing FieldCodec. Context fields are declared once on the
shared NativeContextRecord ancestor and inherited by located NativeContextProof.
The old from_journal mapper, key-set switch and FieldCodec private access are deleted.
Duplicate-key/private-file/revision/generation/digest/tracked-entry corroboration
remain in their existing owners. Reading/locating facts grants no admission/replay.
Existing consumers of located proof fields and six-argument construction remain.

Focused corruption/provenance/retained-generation checks pass. Final installed
pinned-native execution/reopen evidence is being collected in this same PR.
CI deferred; no live deployment/provider calls or original history changes here.
