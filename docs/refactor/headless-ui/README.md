# Toad: a headless core with native frontends

**Heads:** Toad fork `main` at `eeb328da`; `opentui-python` at its last commit (29 March). **Rules:** the Toad package's `00-RULES.md`. Pattern IDs refer to the refactor-audit skill's catalog.

## The idea, and the one refinement that makes it work

Toad's application (agent sessions, turns, transcripts, threads, goals, settings) becomes a **headless core** of typed state and intents that imports no UI library. Frontends render that state natively and send intents back: Textual first, OpenTUI for speed, PyQt later through `pyqt-reactive`.

The abstraction is the **application, not the widgets.** A shared widget toolkit would be the lowest common denominator of Textual, OpenTUI and Qt, which differ exactly where performance and feel live. The core owns state, intents and events; each frontend owns its widgets. `pyqt-reactive` already works this way: its forms are views over an `ObjectState` authority, and the core applies the same principle to all of Toad.

## What the investigation found

**The OpenTUI port** (`opentui-python`, a community port, MIT, dormant since 29 March):
- 38,800 lines of Python (signals, reconciler, components, event loop) over 17 C++ binding files and a prebuilt Zig core, OpenTUI 0.1.91, downloaded from npm. A fork can move to newer upstream cores, which are actively maintained, as long as the bindings follow the API.
- Wheels exist for Python 3.12 and 3.13 only; Toad needs 3.14, so the fork builds its own (scikit-build-core and nanobind).
- Its own benchmarks: a clean frame of a simple box costs about 38 µs, a full paint about 90 µs; but in the text-rendering benchmark, 145 of 185 µs per frame (78%) is the Python render tree. The speed Toad would see on large, streaming transcripts is unmeasured; U6 measures it on the real core before choosing a route. Its reactivity benchmark fails to import in the published release.

**Toad's coupling to Textual,** at `eeb328da`:

| | Modules | Code lines |
|---|---|---|
| Import Textual directly | 152 | 26,744 (68%) |
| Tied to Textual only through other Toad modules | 60 | 7,530 (19%) |
| Already headless | 61 | 5,179 (13%) |

- **The agent-to-UI protocol is Textual's:** 104 Textual `Message` classes, 20 of them in `acp/messages.py`. 136 Textual reactives hold state on widgets, 28 of them in `Conversation`.
- **The indirectly tied logic hangs off a few hubs:** `app` (imported by 11 of the 60), `widgets.conversation` (6), and three small modules that import Textual without any widget, message or reactive (`session_tracker`, `agent_schema`, `setting_choices`). Cutting those frees `agent_controller`, `agent_session`, `transcript_preparation`, `conversation_kind`, `preferences` and more.
- **Already headless:** the JSON-RPC layer, the ANSI parser, key and DEC handling, fuzzy search and terminal execution.

**OpenCode,** the fast tool built on OpenTUI, is itself this architecture: a server, a typed protocol (its `sdk`, `protocol` and `schema` packages), and several frontends over it (`tui`, `desktop`, `web`); its terminal interface renders typed SDK events. It drives the **current** engine, `@opentui/core` 0.4.5, from **TypeScript**: its reconciler is SolidJS on Bun. The Python port pins 0.1.91 and reconciles in Python. OpenCode's speed is therefore evidence for the engine with a JavaScript host, and it opens a third route: a TypeScript OpenTUI frontend as a client of Toad's core, OpenCode's way.

**`pyqt-reactive`** (yours): ObjectState-backed forms for PyQt6, where the state is the model and the form a view over it, with dirty tracking, hierarchy and cross-window updates sharing one authority.

## Surfaces

| ID | Surface | Why |
|---|---|---|
| [U1](U1-core-events.md) | Core events replace Textual messages | the protocol is currently Textual's |
| [U2](U2-free-the-logic.md) | Free the indirectly tied logic | 60 modules, 7,530 lines, a few hubs |
| [U3](U3-view-state.md) | View state moves into the core | 136 reactives; T4, T5 and TC1 are already building these states |
| [U4](U4-frontends.md) | The frontend family | one registry of views per frontend, keyed by state class |
| [U5](U5-terminal-model.md) | The terminal splits into model and view | its parser is already headless |
| [U6](U6-opentui-frontend.md) | The OpenTUI frontend: the Python port in-process, or a TypeScript client of the core | the route is chosen at its start, by replaying a recorded session from the core into both |
| [U7](U7-core-journeys.md) | Journeys against the core | one suite for every frontend |
| [U8](U8-pyqt-frontend.md) | The PyQt frontend, through `pyqt-reactive` | later |

## Order

1. **U1 now,** then **U2**.
2. **U3** together with T4, T5 and TC1.
3. **U4 and U5.**
4. **U6,** choosing its route at the start; **U7** throughout; **U8** later.

Textual stays the only frontend you use until another passes the same journeys. Two frontends implementing one declared family are members, not the dual path the rules forbid, but only one faces you until the second has earned it. Nothing here is a rewrite: the core is extracted from the running application, one hub at a time.

## Decisions

| ID | Question | Default |
|---|---|---|
| **DU1** | When does the OpenTUI frontend replace Textual as the one you use? | When it passes every journey Textual passes, with 95th-percentile frame time at least halving Textual's on a recorded session and input latency no worse |
| **DU2** | What is the core's state authority? | `ObjectState` for configuration (the T1 settings tree, rendered by `pyqt-reactive` in PyQt); typed state families with a typed event stream for runtime state such as turns and streaming transcripts, whose update rate `ObjectState` was not designed for. U3 measures it on the real transcript state before committing |
| **DU3** | Where does the core live? | `toad.core` inside Toad, guarded against UI imports; a separate package once a second frontend exists |
| **DU4** | Which OpenTUI route? | Whichever is faster when U6 replays the same recorded session from the core into a thin view on each: the Python port in-process, or a TypeScript client over the core's event protocol. The TypeScript route runs the engine version OpenCode uses and needs no binding work, at the cost of a second language |
