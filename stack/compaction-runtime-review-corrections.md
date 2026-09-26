# PR95 runtime review corrections — successor to NON-CLEAN 12050b1

This is a bounded **unactivated** correction. Preserve independent exact-byte
reports rather than retroactively clearing `12050b1`:

- `/dev/shm/ac-pr48-runtime-reopen-independent-review-12050b-20260926.md`:
  SIGTERM-ignoring idle child survived a cancelled `discard_for_external_write`,
  while the old handle and `reopen_required` were lost. A separate fake-RPC
  follow-up sent a prompt on corrupt saved JSONL with strict-validator calls
  zero. That is **not** proof that actual pinned Pi accepted corrupt JSONL or
  that a provider was called. A symlink renamed away from `pi-native` also
  passed the verified native launcher resolver but entered the legacy `/compact`
  route (intercepted before mutation).
- `/dev/shm/pr95-12050b-independent-review-20260926/PR95-12050B-LOCAL-PUBLICATION-REVIEW.md`:
  a canonical first→second session switch during an awaited ACP update, before
  the actual transport handoff, delivered first-session commit metadata under
  the second session and marked the first row observed. Corrected report hash
  `c2208d57…`; independent prehandoff probe hash `a59f70ca…`.

The successor sets the irreversible reopen-required marker and expected native
session ID **before** any cancellable cleanup await. It creates an independent
reap task before dropping the old process field; cancellation does not cancel
that task. Every subsequent borrower waits for its completion under the
persistent borrow lock before it can validate/relaunch; errors remain
fail-closed. A test uses a real disposable SIGTERM-ignoring process, cancels
retirement, verifies marker + retained reap task, waits for reaping, corrupts
saved JSONL and proves strict validation rejects before any new RPC spawn.

Canonical manual `/compact` rejection resolves the executable, including
renamed symlinks, before deciding whether the legacy direct-writer route is
permitted. A disposable verified alias test proves denial before launching
legacy compaction.

Local metadata projection now takes a separate ephemeral per-wire kernel
handoff fence; it does **not** hold the registry or wire lock across arbitrary
ACP client I/O. Registry identity changes (PID/incarnation, session file,
worktree, executable role, active status), rename, stop, archive and delete
nonblockingly refuse while that fence is held. Projection revalidates exact
canonical name, createdAt, PID, owner epoch, session file, worktree, ACP session
binding and attached client before the actual update call and again before
marking observed. A changed identity leaves the commit-ID row pending; a
post-COMMIT mark fsync fault may instead leave it observed (see
`compaction-publication-outbox.md`). An independent-process identity-change
control and a deterministic prehandoff ACP callback control prove no wrong
session delivery/ACK for the reviewed first→second attack. A separate
post-delivery ACP binding-change control leaves the original commit ID pending
rather than marking observed. A later dedicated client-only correction (distinct
from the independently reviewed `eedf799` bytes) pins the expected ACP
client/thread at actual `RuntimeServer` transport entry: rebind after the
publisher precheck but before the handoff sends to neither old nor new client
and leaves the row pending. A separate after-delivery client swap confirms
the first delivery **already happened** and cannot be undone; it only prevents
an incorrect observed mark. The registry identity fence does not guard these
ACP-local binding changes. Awaited client delivery still has no deadline,
so identity-change liveness/operational recovery is an OPEN follow-up before
activation; these binding tests do not clear it. This lock is not a
remote UI receipt, recipient selection, global OS sandbox or native writer
replay authority. A client that reports success before actual asynchronous
transport delivery cannot itself establish remote display.

No installed package/live session, provider, network probe or historical UNKNOWN
input is used in these controls. This slice still needs exact independent
review. The production owner adaptive summarizer/trigger and end-to-end ACP
call site remain **open**, so these corrections authorize neither activation
nor merge.
