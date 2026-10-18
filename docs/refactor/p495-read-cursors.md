# P495: conversation read cursors (unfinished)

Based on P493's branch; integrate its merge a59eb6ca1033001342ceb6559cb21585bed61b5a normally before delivery. P493's Covered/Empty DM page proof and its identity errors must remain intact.

## Source facts

- ReadLedger stores ReadCursor values **per Conversation**, but ReadDocument.cursor unions all those conversations into one ReadCursor. Its maximum high_water is then consumed globally. Raising a single conversation's high_water without replacing that projection would falsely mark other conversations read.
- ReadCursor.union/add currently require adjacent **global** sequence numbers and sort all accumulated exceptions. Conversation messages are sparse, so this retains essentially all reads as exceptions.
- OpenedRouteCounts._read_counts and _read_deliveries apply one actor-wide high_water to every sender/target route. They need the cursor of that actual conversation, not the union of unrelated prefixes.
- BusPageIndex.display_unread, DeliveryScope.unread, MessageBus.delivered, DisplayViewer and DisplaySelection also consume that global projection. All must move together; changing add alone is not a safe repair.
- ReadAll.reading fetches MessagePageRequest.every(scope), decodes every matching message and regroups them merely to record reads. An exact conversation can instead use its newest indexed sequence. Aggregate channel scopes still cover multiple actual conversations and must not become one arbitrary last-message conversation.
- DisplayedConversation knows the exact painted references, not whether older messages in the conversation are already read. Advancing a prefix requires indexed proof that no unread gap precedes the shown bound. P493's rule that only fetched references count as painted must not be weakened.

## Chosen change

Keep ReadCursor as the durable cursor of one conversation. Give the existing read-document projection a conversation-aware read object: membership receives the actual message/route, and SQL obtains its cursor once per route. Do not expand compressed prefixes into sequence lists or use a minimum/maximum global prefix as a substitute.

Move contiguous-prefix proof and prefix advancement into the existing displayed-conversation/read-cursor owners. Drop the global-sequence adjacency loop. Preserve ordered out-of-order exceptions without sorting all old exceptions on each mark.

Carry the original durable ledger once outside src, using the page index and each conversation's scope. No compatibility reader or second store remains in production. Copy-root measurements and the actual mark_channel_view_read check come after the coherent producer/consumer migration.

## Evidence and gaps

The existing refactor-audit overlay scanned Core at the P493 branch; stderr is empty. Evidence is in owned scratch p495/before-ast.json and before-ast.txt. Source relationships above were read directly; dynamic registry/SQL callback dispatch is not proven by the AST scan.

The live ledger was read only: 254682 bytes at 12:44. No live conversion, copied-root mark benchmark, source repair or tests are complete. Do not interpret this draft as a working fix. Live conversion remains forbidden until the paired build is installed, with an original-file backup and a single execution.
