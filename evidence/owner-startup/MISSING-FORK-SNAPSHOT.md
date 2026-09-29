# Existing missing child snapshot: read-only findings

Child openhcs-pr159-viewer-bind-owner created1790657026.7285714 (2026-09-29 04:43:46.728 UTC), parent openhcs-architecture-memory, task retained, native session_fileNULL. Parent alone started recovered owner; Carver performed no live start/replay/history write.

Durable parent journal /home/ts/.pi/agent/sessions/--home-ts-code-projects-nominal-refactor-advisor--/2026-09-29T04-00-11-362Z_01a0eb51-f263-778e-b2f4-a2d3a9b65d01.jsonl:
- assistant entry1b704e66, parent566a4f86, 04:43:45.390 contains exact comms_fork request/name/task.
- tool result b74c937c, parent1b704e66,04:43:48.050 reports success, pid3709092.
- later parent assistant7ba1fb70 reports child assignment.
Read365 retained native session headers: none has parentSession equal to that parent journal; no Sep29 04:4x child header found. This bounds the searched location, not every possible filesystem artifact.

Source witness old core3f060 thread_management._fork_unlocked: constructs Thread without session_file, registers it, launches print/model args with prompt=task. NO fork_native_session or parent snapshot operation. Current core339 onward snapshots through native SessionManager before child registration. Thus existing NULL history is consistent with old implementation omission, not evidence current snapshot mechanism failed. Original launch traceback was discarded; startup death cause remains unproven.

The tool call provides exact request provenance, not native child snapshot identity or selected original branch point. Do not copy current parent or invent an inherited branch. No recovery mutation justified by present evidence. Parent fresh actual ACP/paint can establish operational recovery separately from missing inherited history. If historical native snapshot exists elsewhere, need its original session ID/header/source anchor before binding.
