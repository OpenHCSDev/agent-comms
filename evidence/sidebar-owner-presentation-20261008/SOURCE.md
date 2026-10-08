# Sidebar process observation

The active-wheel profile repeatedly invokes ThreadView.presentation. Capturing
a native ThreadView already observes the exact process through
RegistrySnapshot.owner_binding -> NativeThreadExecution.owner_binding. Native
presentation then independently read process_alive again, on every use. External
CLI presentation separately read it twice for marker and summary.

Native presentation now consumes its existing ThreadOwnerBinding. Its nominal
live/unavailable members carry the display answer through native_presentation.
ThreadExecution retains lifecycle precedence and native/external dispatch. An
external CLI participant still has no native binding; it makes one observation
for both fields instead of two independent cuts. No representation, durable
schema, cache, registry, codec or command authority was added.

A held native view displays its acquired owner binding. A fresh view reacquires
process liveness, so a later exit/replacement changes the next publication.
Commands continue to acquire current registry/process authority. Presentation
must not combine an older live binding with a newly observed exited marker.

The complete internal presentation call family was migrated. Core's two direct
ThreadView test constructors were checked: the actual process-bearing status
consumer now uses capture; the lifecycle test now acquires actual ObservedActivity
instead of its obsolete raw Activity. Toad's authored right_comms_pilot constructs
process-free views and is unaffected. Production Toad consumes the same
ThreadView.presentation member and existing worker boundary; no peer edits.

AST source enumeration across Core/Toad src/tests/tools parsed without omissions.
Detail: /home/ts/.cache/agent-scratch/mfc01/sidebar-owner-presentation/owner-consumers.txt.

## Affected checks

- A real private registry and child lifetime check passed: a captured view and
  original FieldCodec roundtrip retain the same presentation after child exit;
  fresh acquisition reports Owner exited. Six other existing checks passed.
- Five broader status checks refused the uninitialized private bus before the
  affected presentation. Their raw failures remain held; no suite pass claimed.
- The obsolete Activity lifecycle test initially refused its missing readiness
  member. Its original consumer now uses captured ObservedActivity; the changed
  control passed in 0.30s. No source guard or runtime policy was weakened.
- An authored real store with 16 active process-bound owners made 16 liveness
  observations at capture and zero during 640 subsequent presentation reads.
  Those reads took 19.4ms under cProfile; this is a scoped private operation,
  not installed wheel latency. All original bindings were retained.

Raw checks/store/cost evidence:
/home/ts/.cache/agent-scratch/mfc01/sidebar-owner-presentation/.

The supplied cross-thread profile has impossible caller relationships across
unrelated functions. Cumulative timings cannot establish main-thread blocking;
the source duplication and real private path are the evidence for this change.
Parent owns the matched installed/UI check. Codec work and remaining poll/layout
cost are not claimed fixed. Parked field_codec.py edits are excluded.
