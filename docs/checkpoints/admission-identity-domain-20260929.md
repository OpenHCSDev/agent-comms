# Admission and registry owner identity

Arendt owns the remaining S14/C0 IDEN-1 domain closure, after merged Core425/431 and Toad211. The installed renamed queue journey is a completed separate receipt and is not repeated by source sanity checks.

## Original defect and relation

QueueScope and CursorScope use RegistrySnapshot.admission_generations; accepted session-load and process recovery use RegistrySnapshot.owner_generations. Live comms428 had admission914 and owner950 with the same PID. Core426 corrected recovery to the original registry/process witness, but both counters still occupied OwnerIdentity.generation.

Introduce AdmissionIdentity in existing thread_identity, carrying incarnation and admission_generation. Original queue/cursor/start/queued-input and passive failed-turn evidence use that nominal identity; registry/process ownership remains OwnerIdentity. Delete Thread.owner_identity(int), close every caller, and preserve distinct official ACP session/root versus mutable routing name through existing AttachmentRelation. No extra registry, resolver, cache, caller guard, PID exception, or compatibility reader.

## Ownership crossing

Mendel owns C3 owner_lifecycle/goal actions/thread status and sender-source430. Schrodinger owns source215 and accepted-load recovery426. Einstein owns TurnSession and native deadline431. This closure changes identity declarations and their current admission producers, not their independent state authorities. Parent owns quiet paired installation and C0 seals.

## Acceptance and limits

Use bounded existing identity/queue/cursor/native ownership checks with actual differing allocation domains and negative birth/process/admission/reordered receipts. Current own-format scopes change in lockstep; preserve durable history and unknown originals. Reuse the existing installed rename/busy queue/native-start/paint journey for the affected paired gate, in a serial native slot after other owners. CI deferred. No paid calls or live input replay.

Worktree persistent under /home/ts/wt/comms-admission-identity-domain-20260929. Disposable output belongs to Arendt under /home/ts/.cache/agent-scratch/admission-identity-domain-20260929; no large build or test fleet.
