# One comms application, Textual and Qt backends

## Work in progress

Tristan asked for a concrete implementation plan, split into phases that can be followed largely mechanically. P144 owns that planning work. The backend is not being implemented yet.

The old P142 read-through is evidence of the paths inspected, not an implementation recipe. Its Qt checkout was stale: **current Qt main already fixes both the conversion-call mismatch and the duplicated nested path. Neither is implementation work here.**

Target current mains of our own packages. Refresh the sources before deriving a change. The commit IDs in [sources.md](sources.md) record what was checked; they do not pin future development or authorize compatibility wrappers.

The [ownership and import plan](00-ownership-and-imports.md) fixes the package boundaries used by the phases below.

## Outcome

One application owns connection, conversation, command, history, navigation, form and preference behavior. Textual and Qt inherit that behavior and implement their native controls, layout, input, painting and resource lifetime. A future renderer must not need another copy of the application.

Existing Core permissions, channel/thread identities, command declarations, message pages and read proofs remain authoritative. Existing ObjectState, Formbridge field declarations, nominal families and Qt infrastructure are extended rather than wrapped by a second model or manager.

## Implementation order being resolved

| Phase | Complete change | State of the plan |
|---|---|---|
| [1. Shared form behavior](01-fields-and-forms.md) | Bound fields, concrete/optional/inherited input, validation, finishing, filtering, dirty marking and model following; both native form users migrated | Literal/inherited input meanings and validation owner resolved; exact Qt bases, constructors, NRA steps and full caller list still being finished |
| [2. Application runtime and events](02-runtime-and-publications.md) | Shared event/subscription lifetime and native delivery, with persistent async execution and a defined shutdown order | Current Qt per-operation executor is not a persistent application runtime; Qt-loop option under investigation |
| [3. Core connection and commands](03-connections-and-commands.md) | Original local/remote observation/capture families and command drafts use shared host capabilities without Textual bases | Source flow traced; module placement decided in the ownership document; full caller changes still being written |
| [4. Navigation](04-navigation-and-selection.md) | Original tab identities, saved order, back/return history and sidebar selection; native focus and widget creation separated | Current logical owners traced; full list of native calls still needed |
| 5. Conversations and history | Original send/outcome/page/read-evidence behavior with separate native rendering and actual-paint reports | Wire path traced; shared message representation and complete native transcript changes still needed |
| 6. Qt application and parity | Qt composes the shared owners, connects its existing widgets and uses the same Core requests; Textual continues using those owners | Starts after the shared families and runtime are settled; no separate app prototype |

Phases are complete caller-family changes, not individual method moves. Forms and runtime investigations can be independent, but their commits must not leave two semantic implementations alive. Later phases may be combined where their source/MRO proof says they must land together.

## What every phase document must contain

The planning work fills these details from source; they are not homework passed to the implementer:

- The behavior and supported cases, including failures, late replies, closing views and independent future changes.
- Existing determining declarations, exact methods and every consumer that changes.
- Destination modules, public parents, concrete members, capability crossings, constructor order and important lookup winners.
- Ordered mechanical changes, original bodies to move, caller/import changes and the superseded path to delete.
- Actual available NRA operations and a bounded unsupported gap where the tool cannot yet prove a move. No imaginary codemod or disguised hand move.
- Existing tests that protect each requirement, new boundary cases needed, source/import/MRO checks and the real consumer run.
- Package declarations and data-reader consequences, if any. No version negotiation between our own components.
- A finish that can be checked from the code and behavior, rather than a count of files or green syntax checks.

A phase is ready only when its remaining work is execution and verification. An unresolved architectural decision is named beside that phase and prevents a ready claim. A change in main triggers a focused recheck of affected declarations, not a frozen old package set or a ritual full audit after every edit.

## Settled boundaries

- **Core:** identity, permissions, membership, command availability/execution, routing/write checks, messages/transcripts, pagination/read evidence and stored ledgers.
- **Formbridge/Formgen:** reusable UI meaning—field/form behavior, generic draft state, event/resource contracts, traversal, model following and other generic application behavior. It imports neither Qt nor Textual and must not import Core merely to draw a comms form.
- **Comms client behavior:** preserve the original Core-specific connection, conversation, capture, navigation and command owners while removing their native host dependencies. Select their exact module placement from the import graph; do not duplicate them into a new facade.
- **Backends:** native widgets, input/event translation, focus/clipboard, layout/measurement, painting, UI-loop delivery and native destruction.

Objects keep their own state. Capabilities compose through multiple inheritance where requirements cross. A nominal class is not permission to copy an existing record or to move a type switch without deleting it.

## Preservation cases already established

- A DM retains its peer's thread birth; a later occupant of the same name receives nothing from that old view.
- A replaced connection cannot accept a stale response or a write captured from the old source.
- An acquired read may finish after observation interests stop; local/remote and answering-only behavior retain their crossing families.
- A bus send with an unreadable result remains unknown; the UI does not replay it to get a cleaner outcome.
- A loaded message is not a read message. Native paint must retain the original page evidence, and Core validates exactly that evidence.
- A draft survives catalog refresh; stale replies cannot overwrite it, and editing clears confirmation.
- Empty plain text, an optional absence, an inherited value and incomplete native text are not merged simply because a widget currently displays them similarly.
- Saved tab scope/order and settings have real readers. Identify those readers before changing durable formats.

## Planning completion

P144 finishes when the phase documents have concrete ownership/caller/deletion/check lists, current-main findings have replaced the stale assumptions, the runtime and shared-field decisions are resolved, and a peer can follow the first phase without inventing an architecture. Source inspection and proof limits remain explicit. Documentation checkpoints land on main while that work continues.
