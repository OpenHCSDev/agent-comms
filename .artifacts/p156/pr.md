## P156: derive cold display counts from the page index

Both full-prefix `public_records` paths in BusDisplayIndex are deleted. Cold/new-scope/read-change snapshots aggregate the exact captured scope in SQLite. Unchanged semantics retain their checkpoint; only the appended tail is decoded and observed, selected through indexed byte offsets.

BusPageRow now stores the existing message's timestamp, human classification (derived from ThreadRole.executable) and resolved mention names. This is metadata in the existing index, not another count cache. ChannelDisplayScope owns its SQL inclusion predicate alongside its authoritative message predicate, preserving target unions, any-mode sender/DM membership and resolved mentions. Timestamp maxima retain the current activity contract, including non-monotonic imported timestamps. Viewer aliases, sparse read exceptions and the opened byte cut are honored. The now-unused ReadCursor.changed implementation and its import are deleted.

### Source evidence
- Base current origin/main ab3848214; isolated branch fix/p156-indexed-display-counts.
- 3 src files, 49 insertions / 52 deletions, net −3 at checkpoint preparation.
- `grep -c 'public_records(' src/agent_comms/bus_display_index.py`: 0.
- Before/after AST evidence in `.artifacts/p156/`; production src/tools census, tests excluded/not run. Lexical sites do not prove dynamic dispatch or behavior.
- Existing owners: Message supplies fields; BusPageIndex alone builds/queries metadata; ChannelDisplayScope determines inclusion; DisplayMetricScope determines viewer/read membership; BusDisplayIndex owns its existing checkpoint. No background warmup, size cap, try/except or second count cache added.

### Not yet accepted / installation risk
**DO NOT INSTALL without the coordinated disposable page-index schema reset and all old-schema consumers being upgraded.** The strict schema intentionally does not tolerate old columns. No live root/store has been changed. Codex owns runtime installation/reset/restarts, preserving sessions and UNKNOWN inputs.

Private cold timing and real plain-st #comms/DM count comparison remain unfinished. /home has only ~2.3GiB free; a new 9GB copy is unsafe. Asked the existing private-source owner for a retained authentic copy; no full live-bus scan or live-root viewer was used. No tests run. This is a draft checkpoint, not behavioral acceptance.
