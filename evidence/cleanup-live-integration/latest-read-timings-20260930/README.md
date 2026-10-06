# Latest original live read timings

Observed: 2026-09-30T19:50:34.626342+00:00. Original comms428 journal and native request diagnostics; read only.

| Read call UTC | File | Model request | Call-to-result journal gap |
| --- | --- | ---: | ---: |
| 2026-09-30T19:30:08.050Z | src/agent_comms/field_codec.py | 18.057s (finished) | 2.260s |
| 2026-09-30T19:30:08.050Z | tests/compaction_retention_fixture.py | 18.057s (finished) | 2.345s |
| 2026-09-30T19:30:08.050Z | tests/test_compaction_retention_fixture.py | 18.057s (finished) | 2.348s |
| 2026-09-30T19:33:57.033Z | docs/refactor/retained-task-memory/01-INDEX.md | 6.655s (finished) | 0.769s |
| 2026-09-30T19:37:23.706Z | docs/refactor/retained-task-memory/S2-MEMORY.md | 12.703s (finished) | 0.776s |
| 2026-09-30T19:45:22.112Z | docs/refactor/retained-task-memory/S4-EVALUATION.md | 10.968s (finished) | 0.717s |
| 2026-09-30T19:46:15.029Z | docs/refactor/retained-task-memory/01-INDEX.md | 8.217s (finished) | 0.829s |
| 2026-09-30T19:49:14.368Z | plans/adaptive-compaction.md | 16.001s (finished) | 0.750s |

These are separate measurements. The journal gap includes the managed tool and persistence path; it cannot isolate filesystem work. Assistant message creation precedes model generation and must not be used as a tool-start timestamp.

Longest completed observed model request: 120.935s, first delta 36.423s, native callbacks 145.753ms total / 7.912ms maximum. This rules out the measured native callbacks as an explanation for that duration; it does not identify provider queue versus reasoning or generation.

Arendt owns tool execution/result-publication allocation through Core #456. Kepler owns pending-input visibility through Toad #251; Heisenberg continues viewport/tab performance through #249.

No public inputs, paid/provider calls, owner restarts, or live state writes were performed.
