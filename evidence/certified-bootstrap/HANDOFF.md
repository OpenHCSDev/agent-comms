# Certified bootstrap and source coverage closure (L0)

Code-bearing draft from parent2298aa4419. Target branch
refactor/canonical-bus-retirement-20260928. Preserve parent's RegistrySnapshot
publication checks and later6422dcb unread cache namespace when integrating.
Parent owns candidate_maintenance and quiet installation; this branch does not
edit it or any live root, provider or installed package.

Fresh Publisher initialization now writes claims+current checkpoint before
committing its pending registry guard. Existing checkpoint builder takes the
caller's already-held bus lock via _bus_locked; no second builder. A failed seal
cannot expose a committed registry owner. Genuine empty source is certified at0.
Separate Messaging/Publisher claim initializer and WireLog.enable_claim_gate are
deleted with callers. Publisher initial attestation uses the certificate only.

Native source witness is PrefixWitness only. Proven coverage reads bounded
certified addressed pages only:8MiB hashedtuple and1000-row whole-bus branches,
their caps and opt-in tests are deleted. Existing100/page and32-page work budgets
remain; source certificate still proves neither selected claim nor native input.
Admission floor starts scanning afterH and never fabricates nativeproof atH.

ACP attachment/session/input effects require configured certified root instead
of accepting publicNone. Obsolete _private_session_mode and bind_owned's unused
mode/fresh arguments removed, directly affected compaction dispatcher no longer
launches its uncoordinated public path. Unconfigured session fails before owner
creation; live owner attachment retains proxy behavior. Manual canonical owner
compaction remains and its directly affected test callers are updated.

Initial meaningful evidence: fresh realfilesystem initialize/publish succeeds;
26 checkpoint/cutover cases pass, one archived write-error ordering failure found
and fixed. Six bootstrap/guard/archive checks now pass: empty certificate,
selected-without-nativeproof gap, seal-failure pending registry refusal,
unconfigured ACP refusal, zero oldreader/API guards, archival read+write refusal.
Pinned native coding/cursor, retained current root and remaining directly affected
native/ACP fixtures are next; this draft does not yet claim those acceptance runs.

Stores: private_bus_checkpoint.sqlite3 is the existing source-derived certificate
owned/sealed by WireLog; no new store, no automatic recreation of old roots.
Fresh bus.jsonl/metadata/registry guard use current formats only. Existing durable
wire/native/user history stays untouched. One-shot retained checkpoint certification
is still available to parent's tools/cutover; it uses the same builder. No new
cutover tool is added. Existing runtime coordinator/native proof semantics stay.

Nietzsche248 informed of fixture crossing: test_acp.py only removes old explicit
claim initializer; associated native/cohort tests receive the same deletion.
Parent owns249 activation and has installed schema-derived cache namespace in250;
this batch leaves view_unread untouched.
