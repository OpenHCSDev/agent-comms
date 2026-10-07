# Display checkpoint reuse

Selected source normally joins integrated Core05c35409e in the existing Mendel
checkout. The installed worker profile supplied by Parent identifies
viewer_snapshot -> display_view_metrics -> BusDisplayIndex.snapshot ->
OpenedWireSnapshot.public_records -> _iter_jsonl_stream. This source change has
not been installed or measured in the public UI.

## Required answers and the discarded work

The old display checkpoint treated the complete DisplayMetricScope as one
invalidation key. Any difference reset both metrics and decoded the entire prefix:

- Sparse read additions or removals: unread changes; activity does not.
- Viewer alias closure: sender exclusion changes unread; activity does not.
- Channel targets, any-mode or current member/alias closure: inclusion can change
  both metrics for that channel. Sender, DM peer and mentions remain included.
- Channel addition/removal or tuple reordering: old code rebuilt all channels;
  only new/changed channel predicates need rebuilding. Removed channels need no
  history, and order alone changes neither answer.
- DisplayBasis is compare=False: it never invalidated this projection.
- Unrelated registry status/heartbeat facts are absent from this key.

AppendCheckpoint separately owns source acceptance: malformed record/integrity,
inode replacement, truncation, changed full-size revision or prefix fingerprint
refusal requires rebuilding. Original wire acquisition still independently owns
retained admission and source certification. Append-only growth needs the suffix.

ChannelDisplayScope.same_projection already declares inclusion independent of read
progress. DisplayCheckpoint.rebase now uses that owner per channel, retaining
activity independently of viewer sender exclusion and sparse reads. Compatible
unread counts change by new.unread(message) - old.unread(message) for the symmetric
difference of seen sets, so losing read evidence can increase a count. ReadLedger
still selects facts by viewer incarnation, bus identity and current conversation
participants; no maximum-cursor or monotonic-read assumption was introduced.

A changed inclusion predicate or viewer alias closure recomputes affected answers
from the old prefix. If a full prefix is already needed, that one scan also applies
other channels' read deltas. The appended suffix contributes once to every current
answer. Checkpoint dictionaries are copied; the prior captured result is untouched.
The original schema2 fields, integrity and atomic writer are unchanged; an old
reader can still consume the new checkpoint and rebuild on a changed scope.

## Source and transaction ownership

OpenedWireSnapshot owns the original descriptor and bounded public projection.
Its optional before_offset separates retained prefix from suffix. Its sparse
reader validates every row on uncertified cuts. CertifiedOpenedWireSnapshot alone
borrows existing BusPageIndex offsets: a read-only SQL transaction captures source
coverage and selected rows together, then closes before JSON/message projection.
BusPageSource.covers and BusPageIndex.record retain original inode/tail/row checks.
Missing, stale or unavailable page evidence falls back to the same source cut; no
index creation, sync, new cache, new protocol or admission bypass is added.

BusPresentation remains the sole display consumer, HistoryViews its caller.
MessagePage continues its existing page/index preparation. BusActivityIndex's
per-target clocks and BusRouteCounts do not replace any-mode display inclusion;
ReadLedger remains the sole human read authority. No other consumer signature
changes. AST enumeration parsed 758 src/tests/tools modules with zero omissions;
related public-record, projection and read consumers were inspected. Dynamic
out-of-tree callers are not qualified by that enumeration.

## Checks and cost

Original authored private stores and original wire/page/read owners were used.
Message.from_wire was wrapped only to count actual decoding, not replace behavior.

- Eight focused viewer/index checks passed in 9.41s, including the new sparse-read
  check: two read receipts in a 100-message store decoded exactly two message rows,
  kept activity/channel clocks equal, returned unread98, then appended unread99.
- Missing page coverage check passed in 0.79s: ten rows decoded, unread9, unchanged
  clocks, and no page-index file created.
- Extended uncertified unknown-observation check passed: sparse selection still
  refuses the unknown original record.
- Viewer removal/new declaration check passed: a fresh same-name incarnation loses
  the old read receipt and unread increases, while activity remains unchanged.

Raw logs/stores: /home/ts/.cache/agent-scratch/mendel-display-read-progress-20261006.
The first pytest invocation stopped before collection on inherited xdist options;
serial addopts selected afterward. The first incarnation check incorrectly tried
metadata registration, whose original owner correctly preserves identity. That
failed raw remains held; the corrected check uses canonical removal/new declaration.
No production change was made in response to that fixture mistake.

The measured saving is message-row decoding work, not live latency. Genuine scope
changes and unavailable/stale page coverage still require a prefix scan. Existing
read-ledger/checkpoint serialization and other sidebar work remain unmeasured.
No App, provider, public input, installed-prefix mutation or rebuild occurred.
