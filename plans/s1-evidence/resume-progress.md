# S1 resumed implementation — 2026-09-27

Acknowledged latest dated directive and queued ownership update in dispatch README.
A8 retains LockedStore/goal persistence; parent retains native_pi, coordinated_runtime,
diagnostics, cursor methods/history; S2 and coordination_store remain untouched.
Only published source adopted, never PR95 worker dirty lifecycle work.

Integrated origin/main e4cd10e (PR126), then published PR95 60e6cc4 via normal
merges. PR95 integration commit 7914c9f. Sole textual conflict was backend test
reopen coverage, retained using typed assertions. ACP adaptive compaction is after
passive-awareness augmentation and supplies SelectedSummaryAdmission callback.
Optional JSON reader bound and parent cursor methods retained.

Found and migrated two new production dict events on strict-reopen failures,
all new mock stream producers and reopen consumers. Removed manual compaction's
separate event formatter: its abort explanation is owned by ManualCompactionEnd,
with shared parent presentation and existing shared turn settlement.

Own normal native bundle prepared successfully. Build scripts now honor TMPDIR
(default /var/tmp unchanged); own optional native fixtures honor TMPDIR too.
Completed sequential tests: 82 compaction/reopen, 9 native compaction, 17 native
settlement/inbox/interruption (4 mounted Toad skips), 308 core, 60 input, 171 goals,
102 presentation/RPC plus 1 actual-native RPC. Details in integrated-validation.md.
Fresh exact global NRA scan: 79 detectors, zero omissions/findings. All 50 S1 Python
files pass Ruff/Black. Latest exact publication is in publication.json.

S1 implementation is complete on the published integration baseline. Next adopter
should merge normally, retaining later parent/PR95 work; no additional S1 worker
launch or live activation is required by this handoff.
