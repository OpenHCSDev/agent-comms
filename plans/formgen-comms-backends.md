# One comms application, multiple UI backends

## Goal

Put shared UI behavior in Formgen/Formbridge so Textual and PyQtReactive use the same application code. A future web backend should use that code too, without starting a web project now.

This covers the whole comms UI: connections, messages, history, tabs, selection, commands, forms, status, and updates from Core. It is **not** a plan to build a second comms app and put a small common interface over the two copies.

We own Formbridge. If it lacks a needed type, operation, or extension point, fix it there and remove the workaround from its callers. The same applies to the other libraries we own. Do not treat their current APIs as an excuse to leave shared behavior in a backend.

**Status:** first source-backed draft, still being expanded. The form paths and the first connection/event contracts have been read. The complete comms call graph and a complete NRA scan are not finished. Items marked **trace next** are required work, not findings being passed off as proven.

## What a completed change must accomplish

- One declaration for each fact. Backends must not restate field shape, type identity, membership, legal state, validation, command availability, or workflow order.
- Real nominal families own behavior. Consumers call the family; they do not switch on names, types, flags, or another object's absent fields.
- Shared behavior is inherited. Where capabilities cross, compose the required parents with multiple inheritance; do not replace them with forwarding services.
- Empty composition classes are a valid finished result. Backend classes implement only genuinely toolkit-specific differences.
- Moving code is not enough. The old consumer decision must disappear, and adding a case must stop requiring edits to unrelated consumers.
- Existing identities and required behavior stay distinct. Do not merge objects just because they currently have the same fields or bases.
- Formbridge imports neither Textual nor Qt. Each backend supplies its event-loop, widget, drawing, focus, and native-input work through declared contracts.
- Core keeps the rules that also govern headless clients: permissions, routing, stored records, command execution, and what a read acknowledgement proves. Formbridge uses those declarations rather than copying those rules into UI policy.

These are consequences of paper1, especially Theorems 3.2, 4.4, 4.6–4.8, 4.14, and 4.17. A renderer can need a different implementation; that does not justify copying the field's meaning or the workflow around it.

## Sources read so far

The initial map uses:

- Toad `cdc6a761b476bd0b25bd323d88860cf5f7d56fe4`.
- Formbridge `0547625fd956077b3130fb1bad9e942f38783149`.
- PyQtReactive `28b3a699e99262005b3e2fdbf7a133de9893fd5b`, read from the OpenHCS submodule. Its tracked files were clean when checked.
- Core `10042a75a2df045f5ee14c6916464d2c08bbe541` for the surrounding client contracts. The later installer repair is separate work.

Toad and Formbridge were read from the already built package set, not inferred from package names. References below are repository-relative paths at these revisions. Recheck moved lines against the tree used for implementation.

## Keep the shared owners we already have

| Owner | What it already owns | Existing users / evidence |
|---|---|---|
| `ObjectState` | Values, declared field paths, change tracking, resolution, history | `toad/forms/form_view.py:74–81,171–237`; `pyqt_reactive/forms/parameter_form_manager.py:222–310` |
| `formbridge/forms/declared_fields.py:147` — `DeclaredField` | Field identity, labels, help, defaults, bounds, annotation, validation, edits | Toad editors hold this object rather than their own value record |
| `formbridge/forms/declared_fields.py:62` — `DeclaredViews` | A renderer family declares what each member shows | `toad/forms/editors.py:41–78`; `toad/forms/form_view.py:59–81` |
| `formbridge/forms/declared_fields.py:241–253` | Validate a value against its field declaration, then edit the state | `DeclaredField.validate` and `edit` |
| `formbridge/forms/declared_fields.py:264` — `form_pages` | Page membership and declaration order | `toad/forms/form_view.py:201–205` |
| `formbridge/forms/parameter_form_service.py:134` — `parameters_for_state` | Which direct fields are shown and what kind each is | `toad/forms/form_view.py:74–81` |
| `toad/conversation_kind.py:56` — `ConversationKind` | Existing nominal channel/DM/IRC family | Members at `:156`, `:227`, `:288`; full member behavior still needs tracing |
| `toad/comms_root.py:202` — `CoreConnection` | Existing local/remote connection family | `LocalCoreConnection:254`, `RemoteCoreConnection:307` |
| `toad/comms_root.py:362` — `Observation` | Existing observation states | `NotObserving:446`, `FailedObservation:455`, `Observing:597`, local/remote members below them |
| `toad/bus_selection.py:37` — `PickerRow` | Existing picker behavior, including whether a row can be removed | `BusChoice:68`, `LocalBus:104`, `HeldBus:115`, `DiscoveredPeer:145` |

Reuse or move these original declarations. Do not create a second form tree, conversation-kind enum, connection-state record, or backend roster beside them.

## Source-backed work to factor

### F1. Separate comms behavior from the Textual application base

**Read:** `toad/comms_host.py:19`; `toad/core_event_carrier.py:63`; `toad/comms_subscribers.py:10,23`.

`CommsHost` combines Core events, bus choice, and a Textual `ThemedApp`. `CoreEventReceiver` combines dispatch with Textual's `DOMNode`. Even the subscriber contracts use Textual's `AbstractHooksMeta`.

A Qt client needs the same event and observation behavior without becoming a Textual application. Split the original shared contracts and behavior from the toolkit bases; keep Textual message posting, DOM lifetime, and Qt signals in their own backend implementations.

**Family to finish:** shared comms application and event/subscription capabilities, plus the concrete Textual and Qt applications. Keep existing connection and observation families rather than flattening them into a new manager.

**What disappears:** toolkit dependencies from shared contracts and any need to copy the connection/event workflow into Qt. This starts as a dependency-direction cut; it is not yet a claim that every method in `comms_root.py` can move unchanged.

**Proof:** walk the existing MRO and every callback caller before the split. Both applications inherit the shared behavior; neither keeps an alternate implementation. Toolkit imports in shared contracts are the demonstrated problem, not merely the file's size. Paper1 Theorem 4.6 and pattern IMPL-13 guide the factoring.

### F2. Give field editing one shared behavior family

**Read:** `toad/forms/editors.py:41–173,277–313`; `formbridge/forms/declared_fields.py:241–253`.

The shared field already owns validation and the state write. Toad still owns text conversion, the edit/finish/refuse flow, default text, enum/choice display, and optional-value editing behavior inside widget classes. `LineEditor.acceptable` also supplies the field annotation and label directly to `validate_annotation_value`, although `DeclaredField.validate` already owns that question.

**Fact being repeated:** which declaration and path validate this field's candidate value.

**At:** `toad/forms/editors.py:140–150`.

**Existing owner:** `formbridge/forms/declared_fields.py:241–246`.

**Repair:** call the field's owner from the backend validator, then factor the reusable edit lifecycle and value-kind behavior into shared parent classes. Textual input events and Qt signals call that behavior; neither decides validation again. Preserve parsing errors, refusal text, when an edit commits, and optional/choice behavior.

**Family to finish:** shared field-edit behavior, scalar/choice/optional capabilities, and backend controls that inherit the required capabilities and their native widget bases. Search for existing equivalents before choosing new class names.

**What disappears:** backend-owned copies of conversion/commit/refusal decisions that have the same required answers. A native control's editing and drawing code stays native. Pattern IMPL-13; paper1 Theorems 4.6 and 4.8.

### F3. Share form behavior without merging the widgets

**Read:** `toad/forms/form_view.py:59–245` and the shared field/page declarations.

Toad's form walks fields, filters groups, marks dirty fields, finishes pending edits, and subscribes to model changes. Some operations are pure form behavior; others mount widgets and repaint a viewport.

**Family to finish:** the shared form and field/group behavior, with toolkit-specific views implementing their actual display work. A group and a scalar field remain distinct nominal cases. Dirty marking and filtering are capabilities, not a growing list of flags.

**What disappears:** a Qt or future web implementation of the same field traversal, group visibility, and commit workflow. Textual `Spacing`, borders, `Content`, lazy mounting, and Qt layout objects are not copied into Formbridge.

**Trace next:** Qt's build lifecycle, reset behavior, nested-manager rules, and every consumer of `FormStructure`. Decide whether each structure is a needed UI result or a copy of `ObjectState`'s topology before removing it. Paper1 Theorem 4.8 and paper4 Theorem 2.6 supply the preservation test.

### F4. Move command drafts and confirmation rules out of the dialog

**Read:** `toad/widgets/comms_command_dialog.py:43–60,147–165,209–293`.

`CommandDialog` builds the draft from Core's field declarations, keeps prepared choices, gathers arguments, refreshes catalogs, rejects stale replies, clears confirmation after changes, and decides when Apply is allowed. Those rules should not need rewriting for Qt.

**Family to finish:** a shared action-form/draft family using the original field and action declarations; Core-specific action behavior uses that family without importing a widget toolkit. Reuse an existing action family if its contract fits. Do not make Formbridge depend on Core merely to move this one dialog.

**Backend work:** create the controls, display confirmation and errors, collect native events, and run the shared operation on the correct UI loop.

**What disappears:** widget-owned command assembly and confirmation decisions. Keep the same draft identity while catalogs refresh; no second cache of its values or booleans restating command state. Patterns IDEN-5 and IMPL-10; paper1 Theorems 4.6–4.8.

### F5. Move existing connection and observation families, not a second connection service

**Read so far:** the declarations and imports in `toad/comms_root.py`, and `CommsHost`.

The file already has nominal local/remote connections, captured sources, failed observations, and answering-read capabilities. Preserve those distinctions. The local/remote and answering-read needs already cross in `LocalAnsweringReads:828` and `RemoteAnsweringReads:833`; that is useful multiple inheritance, not something to flatten.

**Trace next:** the remaining bodies from capture through request, completion, route change, and shutdown. Identify which host calls really require a toolkit. Shared code must retain the rule that a stale response cannot update or write through a different connection.

**What disappears:** a Qt copy of route capture, request ownership, observation state, or cleanup order. Do not manufacture a generic connection facade that forwards to the old Toad owner forever. Patterns TIME-1 and IMPL-13; paper1 Theorems 4.6, 4.7, and 4.14.

## Full comms map still to finish

Every row belongs in the final plan. A forms-only result does not meet this task.

| Area | Starting source | Required shared behavior to trace | Backend work to separate |
|---|---|---|---|
| Start/connect/switch bus | `toad/comms_root.py`, `bus_selection.py`, `screens/bus_choice.py`, `cli.py` | Connection identity, selection, current-source checks, failures and shutdown | Native app start, dialogs, UI-loop scheduling |
| Channels and DMs | `conversation_kind.py`, `navigation_target.py`, `widgets/comms_chat.py` | Conversation identity, destination, actor and available operations | Native controls and focus |
| Send and draft | `bus_submission.py`, `conversation_submission.py`, `widgets/channel_prompt.py` | Draft ownership, target binding, submission and outcomes | Text editing, clipboard, IME and keyboard events |
| Incoming messages | `widgets/message_history.py`, `conversation_comms_handlers.py`, Core observation types | Message identity/order, changes, delivery/read state | Rich/Textual or Qt text/document drawing |
| History and older pages | `history.py`, `history_coverage.py`, `history_jump.py`, transcript readers | Coverage, cursor identity, gaps, anchors and preservation | Scrolling and locating drawn rows |
| What was actually read | `widgets/message_notifications.py`, history/display-basis owners in Core | Which displayed content may be acknowledged; no replay of uncertain writes | Convert a real viewport into the shared display description |
| Tabs and navigation | `screens/comms.py`, `session_projection.py`, `tab_identity`, `tab_order.py`, navigation owners | Identity, selection, open/close/return behavior and saved state | Native tab widgets and focus transfer |
| Sidebar/status | `sidebar_models.py`, `sidebar_navigation.py`, `widgets/comms_sidebar.py`, Core `ui_model` | Rows, ordering, filtering, status meaning, selection and available actions | Trees, layout, colours and paint |
| Commands and catalogs | `widgets/comms_command_dialog.py`, Core `cli_verbs/target_fields.py` | Action declarations, draft edits, catalogs, confirmation and stale answers | Native form controls and dialogs |
| Preferences | `preferences.py`, `setting_declarations.py`, `setting_effects.py`, shared form declarations | Defaults, field identity, validation, application of changes | Native preference views |
| Event delivery and lifetime | `comms_subscribers.py`, `core_event_carrier.py`, `core/events.py`, worker owners | Typed events, subscriptions, cancellation and completion order | Post to the UI thread; hook native widget lifetime |
| Qt reuse | `pyqt_reactive/forms`, `services/field_change_dispatcher.py`, `bridge`, shared window code | Existing shared contracts and the behaviors Qt currently owns | Qt signals, native models/views, widgets and layouts |

This is a reading queue, not evidence that all these paths are wrong. Phone performance and its captures remain with their current owner. Read the paths needed for this plan; do not turn that into a competing performance investigation or worker repair.

## Required shape of the resulting code

1. **Core declares domain facts once.** Forms and application behavior consume the real typed declarations. No parallel wire model or backend-held permission table.
2. **Formbridge owns reusable UI meaning.** Form edits, action/draft state, selection/navigation behavior, and common application workflows live there or in their existing lower-level owner. Move the original owner rather than declaring its twin.
3. **Backends compose shared behavior with toolkit capabilities.** Local/remote access, form kinds, model subscription, commands, history and navigation are overlapping capabilities. Use multiple parents where the requirements cross. Do not force them into one chain and pay for forwarding elsewhere.
4. **Native mechanisms remain native.** Qt signals, Textual messages, drawing, layout units, focus and clipboard operations have declared backend implementations. Different mechanisms do not imply different application rules.
5. **Objects carry their own state.** No `Thing` plus `ThingManager` deciding what the thing is. Use state members carrying the data that state needs; callers hold the family base.
6. **Families supply their membership.** No duplicated editor, action, event or backend rosters. Reuse the owned registration/family mechanisms; do not copy `DeclaredFamily` into another package.
7. **Decode foreign data once.** A toolkit event or wire payload crosses a boundary into a typed value. Shared consumers do not probe it again with strings, `getattr`, flags or type switches.
8. **Remove the old path in the same change.** No compatibility shims, adapter services, duplicate controllers or temporary permanent facades. Preserve a stored/external contract only when its actual reader has been identified.

Names for new classes are not final until the existing families and complete import graph have been checked. The required ownership and reuse above are not optional.

## How the implementation will proceed

### 1. Finish the map

- Trace declaration → state → change → every consumer for each area above.
- Read the current Formbridge and PyQtReactive design notes and follow their source references. Old documentation is a lead, not proof of current ownership.
- Retain one complete NRA scan for each affected source tree, with revisions and recipe frontiers. No removal totals from partial searches.
- For each finding, record the pattern, repeated fact, source location, deriving declaration, affected consumers and how a new case is added before/after.
- Mark unknown or external paths explicitly. A passing UI test does not make the map complete (paper1 Corollary 4.10).

### 2. Define the shared families before moving code

- Write the required cases and which consumers need each behavior; keep identities, membership and implementations separate.
- Draw the parents and name crossing capabilities. Preserve lookup choices, state ownership, initialization and shutdown order.
- Check existing parents first. Add a genuinely missing declaration only at the owner all its consumers can import.
- Establish the import direction before a move. No new `TYPE_CHECKING` or local-import workaround. Use NRA's guarded-import proof; fix a real cycle by moving the original declaration below its consumers.

### 3. Land complete slices

- Start with the smallest **complete family** whose behavior is already traced in both backends, then take the largest supported batches. Do not do one copied method at a time with a broad suite between edits.
- Candidate first slices are field edit behavior and the event/subscription contracts. Their final order depends on the import graph and the completed Qt trace.
- Move shared behavior into Formbridge and migrate **all** affected callers in the same PR. Delete the previous owner and copied dispatch.
- Use native NRA moves and import updates. If a needed move is missing, take that tool gap to its existing owners; do not hand-move around a refusal.
- Record what was actually removed. A relocated switch or renamed helper earns no factoring credit.

### 4. Build the Qt comms client from those same families

- Compose the shared application behavior with Qt's existing controls and event loop.
- Keep Textual using the same shared parents throughout the move. Do not leave a special Textual implementation as the reference application while Qt grows its own.
- Exercise actual connect, channel/DM selection, message sending, history, read acknowledgement, tabs, commands and preferences through both backends.
- Do not start a web implementation. The shared code must simply have no dependency that would require rebuilding its behavior for a web renderer.

## What proves reuse and correctness

- Shared packages import with neither Qt nor Textual available; runtime annotations resolve without guarded imports.
- Both backends reach the same shared implementation for each shared behavior. Walk their MROs and follow the calls; a matching method name is not proof.
- A new semantic case is one declaration plus genuinely new toolkit implementation where needed—not edits to consumer switches, literal rosters, serialized names or parallel state records.
- One shared behavior test exercises its family. Backend tests drive real widgets/events and state APIs, not hand-written private state.
- Check invalid edits, optional values, unknown choices, catalog refresh races, confirmation clearing, connection replacement, cancellation and view closure.
- Preserve message identity/order, historical gaps, tab identity, display/read proofs and uncertain-write handling. Never replay an UNKNOWN request to get a green result.
- Run touched tests, repository guards and tests naming changed declarations. Preserve existing painted/real-input assertions; do not rewrite them to certify the new internal shape.
- Before deleting or merging a declaration, name the admitted cases and why no merged pair needs different behavior. A failed deletion needs its concrete counterexample, not “overengineered” (paper4 Theorem 2.6 and Theorems 6.5–6.7).
- Report **moved**, **factored**, **merged**, **installed**, and **seen working** separately. The finish is a Qt and Textual comms app sharing their meaning, not a count of new abstractions.

## Next concrete reading

Finish `comms_root.py` and `core_event_carrier.py` through their actual consumers; then follow one complete command-draft edit/apply path in Toad and one complete field-edit path in Qt. Update this same plan on main with the resulting families and exact removals. Do not start a separate unpublished plan branch.
