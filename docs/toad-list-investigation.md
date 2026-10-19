# Toad blank thread list during import

Owner: pi-toad-list. Scope: importer acknowledgement and the MessageBus snapshot lifetime.

## Actual source

Core `ui_model.observation.SidebarReading` reads an initial roster and then
`HistoryViews.viewer_snapshot`. Toad `comms_root.CoordinationAccess.sidebar_observed`
applies that result to Core's `SidebarModel`; Toad's `SidebarModelView` and
`CommsSidebar` render its keyed rows. Native unread preparation is downstream of
that same captured registry, not an independent thread roster. Delivery cursors
filter messages, not registry membership.

`RegistryStore.read_revision_unlocked` reads under its registry shared lock;
`PublicRegistry.write` and `PrivateRegistryGuard.write` publish with
`store_files._atomic_write_text`, using `os.replace` without unlinking the old
registry first. A genuinely missing registry creates the empty initial document;
malformed content raises. `RegistrySnapshot.capture` copies the current maps.
Visibility can intentionally omit stopped/archived/non-executor threads.

A stored read failure becomes `SidebarUnreadable`, not `SidebarObserved` with an
empty roster. Toad clears the failed model and calls `show_error` with the original
cause. Deferred reads publish nothing. Thus a failed sidebar can look blank after
its notification; there is no evidence that this path caused the reported event.
No failed read is changed to a successful empty result by this patch.

## Reproduction on main

Installed Core main at 8b27e02aa and assigned branch base 0280f852e have the same
relevant implementation. Independent registration/sidebar loops on main:

- public bus: 20.016 s, 1,072 observations, 3,808 writes, zero empty/error results;
- private bus: 20.021 s, 1,113 observations, 3,419 writes, zero empty/error results.

Actual import, unlike registration alone, deadlocks. `ThreadManagement.import_thread`
holds root/wire exclusively at line 187 and calls `mark_delivered_through` at line
202. `MessageBus.snapshot` at line 102 opens another descriptor and asks for the
same wire lock shared. The importer blocks itself. The observer waits for wire in
`Messaging.user_identity`, called by `HistoryViews.viewer_snapshot`. Both original
process stacks are retained in the owner's small `import-stack.log`.

`tools/reproduce_toad_import_blank.py` starts real private Core, 85 stopped threads,
a real CLI import, Xvfb and plain st running the installed Toad ACP entrypoint.
It sends no model input. At 15.000 s the importer was still running, the registry
contained 87 entries (85 agents, user, newly imported agent), and Toad's actual
screen showed Channels / New Session with no channel or thread rows. Terminating
only that scratch importer released the wire; at 25.209 s the same Toad showed
`#any 0/86`, `imported`, and sentinel rows without a restart. Original physical
screens are in the owner's scratch `ui-main-acp` directory. This demonstrates an
unanswered observation, not an observation that returned an empty registry.

## Ownership change

MessageBus retains one implementation of coherent registry/catalog/read/source
capture and one delivery-acknowledgement implementation. Normal readers acquire
wire for capture and release it before consuming the opened cut, as before. The
importing writer uses that same capture under the wire lock it already owns.
The import's declaration, acknowledgement and rollback remain within its original
exclusive lifetime; no newly published thread is exposed to a start between
registration and acknowledgement. No retry, retained nonempty roster or sleep is
added to production.

AST evidence used the existing refactor-audit `Package.load` on all 379 Core
production modules, zero parse omissions. Original declarations, wire acquisition
lifetimes and every mark_delivered_through caller are retained in the owner's
`source-before.txt`. Toad's installed observer/model/widget consumers were read
semantically. Generic dynamically bound snapshot calls are not claimed resolved.
NRA's latest ModuleSyntaxIndex could not load with the stack's newer
metaclass-registry; its existing 3.12 environment cannot parse default type
parameters. The working refactor-audit parser uses the project's Python 3.14.

## Evidence custody

Small disposable scratch: `/home/ts/.cache/agent-scratch/pi-toad-list`, owned only
by pi-toad-list, for scratch buses, physical captures and short command results.
No saved user sessions, wire or uncertain inputs were moved, replayed or removed.
Physical captures for failed setup attempts are disposable; retain only the
accepted main/branch comparison and concise logs until review.

## Assumed

- The historical 13:02 event used the installed importer/snapshot code whose
  deadlock is reproduced here. No historical observer stack was recorded.
- Legitimate stopped/archived filters and a truly empty new bus may show no rows.
- The Xvfb comparison verifies the installed Toad and real Core source path;
  the production installation is not changed by this work.
