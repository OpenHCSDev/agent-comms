# PR #2 test disposition for PR #1

Reviewed the 18 test paths unique to PR #2 at `79fe55a9068dd292c0443df52ee71fba38b233b3` against the integrated V3 tree. These decisions apply to the tests, not to PR #2's legacy `tui.py`, which is intentionally omitted.

| PR #2 path | Decision on the current tree |
| --- | --- |
| `test_active_turn.py` | Ported; active work must remain busy until its turn finishes. |
| `test_activity_index.py` | Ported; incremental activity parsing, replacement, partial-tail recovery, and expiry still apply. |
| `test_auto_title.py` | Ported; one concise title remains the owner behavior. |
| `test_channel_display.py` | Ported and adapted the mode-expansion unread assertion: a formerly hidden, unpainted DM becomes unread when the mode expands. The channel's per-view mode control remains valid. |
| `test_detached_owner.py` | Ported; the reservation fixture now consumes the inherited proof pipe used by current owner startup. |
| `test_goals.py` | Ported persistent goal, explicit resume, and self-set tests. Dropped the two synthetic `/bin/echo` continuation tests: they bypass the current private grant and native Pi start requirements. Current ACP goal tests cover authorized continuation, completion, failure, and no replay. |
| `test_project.py` | Ported; project switching and Pi bootstrap still apply. |
| `test_prompt_queue.py` | Ported queued and steered followups. Dropped the cancellation-restores-queue test: an uncertain input must remain durable UNKNOWN and must never be queued for automatic replay. Current input-disposition tests cover this outcome. |
| `test_registry_revision.py` | Ported; cached reads must notice external writes and reject malformed replacements. |
| `test_reply_routing.py` | Ported; live and saved delivery routes remain distinct. |
| `test_restart.py` | Ported and adapted the fixture to the current two-phase owner stop and admission checks. |
| `test_start.py` | Ported and adapted the local-participant fixture's `wait` argument. |
| `test_thread_order.py` | Ported; selection and rename must preserve sort metadata. |
| `test_thread_unread.py` | Ported; transcript indexing, source replacement, and human read positions remain valid. |
| `test_tui.py` | Dropped: it tests the removed legacy `agent_comms.tui`. The mounted Toad UI is tested in its own repository. |
| `test_user_channels.py` | Ported; member wake and reply routing remain valid. |
| `test_user_view_mark_read.py` | Ported; human mark-read must not acknowledge an agent inbox. |
| `test_visibility_views.py` | Ported; stopped and archived threads remain explicit view filters. |
