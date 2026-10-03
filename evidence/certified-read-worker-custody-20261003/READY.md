# Ready:563 certified read and turn publication custody

Production source4c30bd2336d841f9592ad443126e66d58257ec1a.
Three existing production files20 added/8 deleted; no native or schema changes.

- WireLog closes the acquired read file inside its original joined worker,
  after source validation, before returning the detached result. The event loop
  cannot be required to resume to release a completed read's physical lock.
- TurnProgress joins committed-progress publication and terminal success/failure
  publication, annotation and checkpoint before ACP delivery or lease release.
- TurnRunner relay joins the original send and derives sequence from its Message;
  the independent global last-sequence read is deleted.
- Original per-input receipts, currentness, publication/lease fences and inherited
  process custody remain. No reentrant lock registry, new class, timeout, shared
  lock substitution, source/state mirror or additional replay path.

Before/after NRA AST:311 production+361 test+53 tool modules, no parse omissions.
Candidate attribute references need semantic resolution; see SOURCE.md for the
read/publication/response family and intentionally retained ingress lifetimes.

## Qualification

Source controls2PASS1.47s. Installed wheel controls2PASS1.57s, using the same
existing custody controls and real private CommsAgent/OwnedTurn/registry/input,
wire certificate, notice/diagnostic and checkpoint owners. Supplied native events
are control inputs; no provider response or actual native answer is claimed.
Completed worker vs undelivered Future, pending acquisition, joined cancellation,
original callback refusal and publication-before-lease-retirement are exercised.

Released485 holder reused, normal `uv build --wheel` and `uv pip install` with
all declared dependencies;69-package `uv pip check` passed.339 production files
and every wheel application member match,0 differences. SDK0.12.1. Native manifest
de16647979cbad28be4f60349d39a420e83aaffb1d678941609449d306109968
matches source and installed asset. No new environment/worktree/native copy or
public default change. Existing pytest tooling is borrowed from534; application
imports remain the installed wheel (no source/PYTHONPATH overlay). See
installed-source-proof.json and acceptance.json. Holder is private Core-only
qualification; old archived activation does not qualify a paired release.

Attempt receipts: initial source command inherited unavailable pytest plugins,
then the534 interpreter initially loaded its older installed Core and lacked the
new idle method. Neither reached candidate qualification. Explicit source sanity
and the339-file-verified installed candidate checks above subsequently passed.
Released69 holder lacks pytest; installed controls borrowed existing pytest
without installing another package or environment. These were setup limitations,
not hidden candidate pass claims.

## Original execution and remaining boundary

Arendt560 originalPID1583616/birth46995415, native finalb7807f3e and BUS inode
17965242 heldEXfd11/waitEXfd10 remain protected. No signal, fd closure, hotpatch,
restart, provider call, input send/replay or original-store mutation occurred.
The new source does not retroactively release that already-running old lock.
Arendt owns original attempt disposition; parent owns reviewed release. No claim
of recovered original ACP return, solved55513.696s or live latency improvement.
Separate frozen562 artifact9783899/0446467 is not mixed into563.
