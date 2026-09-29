# Sender outbound transcript closure

Owner: Mendel, backend contributor to Schrodinger Core210 paired integration.
Base main7995bc5a. Separate from C3 Core421 and selected-summary fixtures Core422.

Concrete user witness: sender DM shows inbound19:26:44 codex-bootstrap to
openhcs-pr159-viewer-bind-owner, but omits that owner's outbound19:27:10 reply
beginning "Role mismatch: this thread owns OpenHCS PR159(viewer binding)".
The canonical durable IRC/wire already contains that outbound message.
Screenshots inspected: /tmp/codex-clipboard-sFX8bA.png and
/tmp/codex-clipboard-l84QXh.png. Do not rely on clipboard paths as durable evidence.

Derive the original authoritative wire record through the shared source
projection/index/publication relation. Trace both inbound and outbound and close
all consumers. No sender-only append, duplicate message, seen cache, new store,
status mirror or alternate history. Existing source updates own refresh and
invalidation; no second polling authority.

Coordinate shared methods with Schrodinger before coding. He owns sender DM/UI
integration; Mendel owns backend source routing/index/publication closure.
Acceptance: actual native/tool publication plus ACP/UI, exactly-once immediate
own outbound, chronology, cold reopen and A/B/A. Reuse real private retained
fixtures/isolated Xvfb as needed; loopback provider only. No paid calls, replay
of owner inputs, live-root writes, global install/restart or desktop pointer use.

Persistent scratch owner Mendel:
/home/ts/.cache/agent-scratch/comms-sender-outbound-transcript-20260929
for bounded receipts and screenshots. Source worktree under /home/ts/wt.
