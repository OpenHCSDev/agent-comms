# Ownership and import direction

## Settled placement

Use the existing repositories. Do not create a FormGen repository, a new client service or a second application.

1. **Formbridge owns toolkit-independent form/application mechanisms.** Field metadata/validation, shared input/form algorithms, publication/subscription lifetime and generic native-work contracts belong here. It does not import Core, Toad, Textual or PyQt.
2. **Core owns the bus.** Keep commands/effects, permissions, identities, routes, source captures, message/page/read evidence and durable stores at their current Core owners. Do not move these into a UI library.
3. **`toad.client` owns the shared comms application behavior.** Create this namespace inside the existing `batrachian-toad` distribution. It may import Core, Formbridge, ObjectState and generic utilities, but no native UI package or renderer module.
4. **The original Textual modules and `toad.qt` implement native views.** Add the Qt backend inside the same distribution, using PyQtReactive's existing controls/form support. PyQtReactive remains a general Qt library and does not import Toad or Core merely to supply a widget.

Putting shared code under `toad.client` makes its import check automatic. A test can discover/import the entire namespace and reject native UI dependencies, rather than maintain an ever-growing whitelist of supposedly shared top-level modules. `toad/__init__.py` currently loads no toolkit; preserve that property. Once a shared declaration moves, all original consumers import its new owner; no old-path re-export remains.

Generic and comms-specific behavior stay separate. A FieldPath or generic publication queue has no reason to know a bus root. A captured Core source has no reason to know a QLabel or terminal strip.

## Proposed destination modules

These paths are implementation targets, not APIs claimed to exist today. Move original declarations and their behavior; resolve only their irreducible native calls through the declared native contracts. Each phase lists the exact declarations and callers to change.

| Current owner(s) | Shared destination | Native remainder |
|---|---|---|
| `toad/core/events.py`: stream/subscription/coalescing algorithms | Generic mechanism in `formbridge/publications.py`; Core-specific events and scoped interest in `toad/client/events.py` | Event declarations whose payload really is a terminal rendering block remain in the Textual area; they inherit the shared event base without being re-exported by the shared module |
| `toad/core_event_carrier.py` | `toad/client/event_receiver.py` over generic Formbridge receiver/delivery contracts | `toad/core_event_carrier.py` carries the original delivery through the Textual pump; `toad/qt/event_carrier.py` through Qt |
| `toad/worker_group.py` | Generic operation kind/owner contract in `formbridge/operations.py`; comms operation kinds with their domain owner | Textual Worker construction/group adaptation remains native; Qt task construction remains native |
| `toad/comms_host.py` | `toad/client/host.py` | ThemedApp, DOMNode and concrete app lifetime belong to the native application, not this contract |
| `toad/comms_root.py`, `comms_subscribers.py`, source/read/release helpers | `toad/client/connection.py`, `subscribers.py`, original narrowly owned source/read/release modules beneath `client/` | Native worker start/cancel/delivery hooks, sidebar painting and app succession |
| `toad/conversation_kind.py`, `conversation_submission.py`, `bus_submission.py` | `toad/client/conversation.py`, `submission.py` | Native prompt events, attachment UI, notification drawing, focus and scroll |
| `toad/tab_identity/`, `tab_order.py`, logical parts of `session_navigation.py`, `session_entry.py`, `navigation_target.py` | Corresponding original owner names beneath `toad/client/` | Native screen/widget factories, mounting, focus and modal navigation |
| `toad/target_commands.py`, logical parts of `thread_actions.py` | `toad/client/commands.py`, `thread_actions.py` | Native menu/dialog geometry and native form composition |
| `toad/wire_history.py`, logical history-coverage/traversal ownership | `toad/client/history.py` and narrowly owned history modules | Terminal document/strip publication and Qt document/paint publication |
| Generic field/form algorithms in `toad/forms/` and Qt form code | Formbridge destinations in [phase 1](01-fields-and-forms.md) | Existing `toad/forms/`; PyQtReactive form/control modules and Qt comms form composition |
| Generic preference meaning | Existing declared preference model, with shared effects separated from native effects | Textual/Qt appearance, focus, clipboard, terminal/native title and native layout effects |

Do not move a dependency merely because the table names a shared namespace. First establish whether it is pure, contains native behavior, or is a separately required role. For example `AgentFail.present` currently creates Textual Content/notes inside the event module; moving that module unchanged would preserve the toolkit dependency rather than share it.

## Inheritance shape

- Generic Formbridge mechanisms are public ABCs/capabilities. Shared algorithms live on those parents; a native member supplies its actual drawing/input/scheduling/destruction primitives.
- `toad.client.host.CommsHost` combines the shared publication receiver, connection/tab/session contracts and the original application outcome. It no longer inherits ThemedApp.
- The native Textual app and Qt app compose the same comms behavior parents with their native application base. Native bases do not creep into shared contracts through a metaclass alias.
- Retain the original local/remote and observing/answering-only capability crossings. Captures, read leases and completed/unknown transaction outcomes are not backend flags.
- Keep a class serving a distinct state or contract even when two current implementations agree. Rendering roles can share implementation without merging their identities.
- Empty concrete classes whose base list states the whole combination are complete. Do not fill them with forwarding methods.

The exact application base/MRO and constructor ownership must be listed after the phase-specific import/caller traces. Naming this shape does not establish that a Qt/Textual metaclass combination runs.

## Native runtime decision

The shared application runs its asynchronous work on the native UI thread, with one persistent asyncio loop for the Qt application lifetime. This preserves the current single-threaded ordering between field edits, ObjectState callbacks, view/source changes and queued publications. A second worker loop for the whole app would require a new cross-thread state protocol and is not the selected design.

Blocking Core/helper preparation remains at its original bounded worker/process owner. A persistent application loop does not move CPU work onto the UI thread or allow a blocking wait for a worker result.

For Qt, use `qasync.QEventLoop` through an `asyncio.Runner` loop factory. Phase 2 specifies required fixes at qasync's owner for caller context/cancellable handles, inherited executor ownership and repeated executor shutdown. The inspected source is **not verified working on Python 3.14**. Those owner changes and native controls are required implementation steps before the Qt backend ships, not a demand that this documentation-only task build an unwritten application. Never replace them with a per-operation loop, a nested `processEvents` wait or an extra background application.

## Imports and package declarations

Target order:

`Core / ObjectState / python-introspect / metaclass-registry` → `Formbridge generic mechanisms` → `toad.client` → `native Textual or Qt views`.

The arrow means “can be used by”; Core does not import Formbridge UI machinery to perform a command. Shared client code may use both Core and Formbridge; Formbridge must not acquire the reverse dependency.

Toad currently lists Textual and its image/diff/server packages as mandatory dependencies. A source import-isolation check does not claim a Qt-only installation without those packages. The final package/entry-point phase must decide the exact optional-backend declaration and installer selections together, preserve the normal `toad` entry, and add the Qt entry using the shared startup/argument behavior. Do not claim a lightweight Qt distribution before that declaration is actually changed and checked.

No new environment variable selects meaning or backend behavior. Native entry modules import their own toolkit; common startup must not import both native implementations in order to pick one.

## Boundary checks

- Import every module under `formbridge` and `toad.client` in a separate interpreter with native modules recorded/forbidden; include nested packages, not a hand-kept list.
- Preserve current `test_form_package_loads_without_qt_or_the_comms_package` behavior for generic Textual form use, with its real test name taken from the source test file when executed.
- Grep every moved declaration/import path across source, tests, tools and sibling packages; resolve string entry points/stored readers explicitly.
- Use NRA's complete import graph for moves and guarded-import promotion. A cycle refusal changes the module split; it is not waived by TYPE_CHECKING or a local import.
- A module's source parse, a simulated MRO and an actual native instance are different evidence. Report each separately.
