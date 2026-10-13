Evidence notes for PR #757 (route-count startup rebuild), from the review round.

## Which branch fired on the live bus (read-only evidence, 2026-10-13 ~04:00)

Live route_source row: identity [2083, 4606680, ...], offset 1630245299, tail
digest 4d192a4b5706c267... Live bus.jsonl: st_dev=2083, st_ino=4606680, size
1630808976. Same device/inode; recorded offset <= live size; recorded tail
digest still verifies against the file bytes at [offset-4096, offset). From the
old decision tree (bus_route_counts.py:141-147 + :149-153) this rules out for
this file: first build (recorded row exists, offset > 0), identity change or
replacement (recorded (st_dev, st_ino) equals the live file's), digest mismatch
(the digest verifies). Remaining trigger: size < recorded.offset - a reader cut
behind another reader's sync. The 03:17 py-spy'd rebuild therefore took the
size < recorded.offset branch.

## Append-only owner

wire_log.py:1 module doc: "Canonical append-only wire persistence".
wire_log.py:413 _append_private_unlocked is the only bus writer; :429 opens
with os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW - it can only
extend the file. No truncate call exists in the wire path. A replaced bus is a
new file with a new inode, rebuilt via the (st_dev, st_ino) check.

## Truncation-in-place closure

The fix verifies the recorded tail_digest against the same inode's bytes at
[offset-4096, offset) before answering an ahead cut. Append-only writer keeps
those bytes -> answers. Truncated or rewritten source fails the digest ->
rebuilds, same mechanism as the existing check for cuts at/beyond the recorded
offset. Pinned by
tests/test_current_delivery.py::test_truncated_source_fails_the_recorded_digest_and_rebuilds.

## Failing-then-passing (public path)

test_index_recorded_ahead_of_the_read_cut_answers_without_decoding uses
views.presentation.snapshot + bus.pending_counts_opened (the viewer_snapshot
chain) and patches Message.from_wire (suite precedent,
tests/test_viewer_snapshot_index.py:75). On pre-fix code (HEAD^) it fails:
{'sender': 5} != {'sender': 6} (rebuild answered from the stale cut). With the
fix: 0 rows decoded, counts 6, recorded revision not regressed. Targeted set:
20 passed (taskset -c 4-9 nice -n 10).
