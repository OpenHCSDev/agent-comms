# ACP attachment retirement

Owner: Schrodinger. Scope: SessionLifecycle.attach_owner and original RuntimeProxy lifetime.

## Reproducer

A single official SDK client loads the same live session twice. The second load replaces `proxies[session_id]` without closing the original subscription. One original owner failure is forwarded twice. Arendt reproduced this at `/home/ts/wt/e463-run02/failure-boundary.json`; the failed input and journals are preserved and will not be replayed.

## Required relation

One ACP session has one owned subscription resource. Replacing the attachment retires and joins that original resource before the new subscription is accepted; a failed or cancelled replacement cannot leak an untracked subscription. Original owner/process and load proof remain strict. No aliases, input replay, semantic status copies, or deduplication.

Pattern leads: IDEN-6 (resource overwritten by session key), AGENT-2 (new resource admitted without retiring original). The existing resource map remains the sole lifecycle owner.

## Acceptance

Actual installed official SDK initialize/load/load and original owner notification over the real Unix socket: one notification, retired subscription closed, failed/cancelled load cleaned, original history and input dispositions unchanged, no provider calls. Preserve baseline and candidate proof. This is independent of S14 bus and typed external-error decoding.

## Accepted checkpoint

Production: 21 deleted / 35 added lines, one file. Existing `SessionLifecycle.proxies` retains custody until `RuntimeProxy.close()` cancels and joins the old reader. One lifecycle resource lock serializes replacement and shutdown. New unaccepted subscriptions close in the existing load operation's `finally`, including cancellation. Local-owned binding uses the same retirement operation. Original process/owner load fences and failure witness remain unchanged.

Actual noneditable wheel plus official SDK0.12.1 and trusted native593b: same client loads twice, then one original controlled provider failure produces one notification (original baseline two). Two localhost requests produce two original native inputs. Fresh saved-history attachment leaves original native bytes and input dispositions unchanged. Zero paid/public calls and zero replay. Actual Unix invalid-ready and cancelled-subscription boundaries: 2 pass. Required debt ratchet: exit0, zero increases. Exact original/source and notification receipts are under `evidence/acp-attachment-retirement/`; private originals remain `/home/ts/wt/g465/u01` and `/home/ts/wt/e463-run02`. No broad bus/UI readiness or public activation is claimed.

Reuse existing actual installed journey with `--repeat-load`; no duplicate test driver remains.
