# L0B canonical bus retirement — implementation in progress

Owner: parent. Branch `refactor/canonical-bus-retirement-20260928`.
Round-two scope includes L0 part B and unassigned L0A callers; canonical rules
and the owner's D22 decision are in merged PR227 docs/refactor/round2.

## Actual changes

- Deleted Publisher.publish and WireLog's old append/sequence methods.
- Ordinary publication initializes a truly fresh root under the existing lock
  and uses the current audience/decision writer. Existing unmarked data fails
  explicitly instead of being appended through the old protocol.
- Deleted the old ACP ACK/steer inbox execution branch.
- Deleted MessageBus.mark_view_read; HistoryViews uses ReadLedger directly.

## Local evidence and remaining closure

`first-local.log`: 19 passed, 5 failed. This is not ready to install.

- Two tests assert the deliberately removed unmarked writer/API; replace those
  with one fresh canonical-send behavior and an old-data refusal check.
- One current read-ledger test calls update_tags, whose writer migration is
  owned by Darwin B2. Integrate that PR; do not re-add the old writer.
- Two current DM read tests expose required agent-to-USER send parity: the
  existing initial-cohort writer accepts only executable direct recipients.
  Resolve using current publication ownership, with no second write path.
- Remove dead public-delivery fields, queue admission/ticket branches and tests
  made unreachable by the drain removal; retain current direct ACP/goal queues.
- D22 is approved: one rewrite into current format, history preserved in place.
  Inspect original/attached durable stores; prepare a tools/cutover tool, test
  copied real data and preserve IDs/order/body/provenance. Runtime resets must
  never re-admit old messages or replay uncertain inputs.
- Once history is rewritten, remove pre-cutover row readers and
  supervised_cutover.py entirely. The one-shot tool must also be deleted after
  it runs at the owner's quiet install, before this surface is called complete.
- Full scoped L0 guards and integrated local suite remain to run. No installed
  behavior claim is made for this draft; live installed bus is unchanged.
