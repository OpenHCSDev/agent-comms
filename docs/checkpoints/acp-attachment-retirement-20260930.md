# ACP attachment retirement

Owner: Schrodinger. Scope: SessionLifecycle.attach_owner and original RuntimeProxy lifetime.

## Reproducer

A single official SDK client loads the same live session twice. The second load replaces `proxies[session_id]` without closing the original subscription. One original owner failure is forwarded twice. Arendt reproduced this at `/home/ts/wt/e463-run02/failure-boundary.json`; the failed input and journals are preserved and will not be replayed.

## Required relation

One ACP session has one owned subscription resource. Replacing the attachment retires and joins that original resource before the new subscription is accepted; a failed or cancelled replacement cannot leak an untracked subscription. Original owner/process and load proof remain strict. No aliases, input replay, semantic status copies, or deduplication.

Pattern leads: IDEN-6 (resource overwritten by session key), AGENT-2 (new resource admitted without retiring original). The existing resource map remains the sole lifecycle owner.

## Acceptance

Actual installed official SDK initialize/load/load and original owner notification over the real Unix socket: one notification, retired subscription closed, failed/cancelled load cleaned, original history and input dispositions unchanged, no provider calls. Preserve baseline and candidate proof. This is independent of S14 bus and typed external-error decoding.
