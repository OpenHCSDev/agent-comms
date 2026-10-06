# U6: The OpenTUI frontend

**Index:** [README.md](README.md). **After U4 and U5.**

## Choosing the route (DU4), first

Replay a recorded session from the core (U1's wire forms make every session replayable) into two thin transcript views: one through the Python port, one in TypeScript on `@opentui/core` with its Solid reconciler under Bun, fed over a pipe. Measure frame time at the 50th and 95th percentile, input latency, and CPU per streamed token, in a real pseudo-terminal. The faster route is the one built; the other view is deleted.

## The two routes

- **In-process, through the Python fork:** `OpenTUIFrontend` inside Toad's process. The fork needs Python 3.14 builds (scikit-build-core and nanobind), the broken reactivity benchmark fixed, and its pinned core (0.1.91) moved toward upstream's current release.
- **A TypeScript client, OpenCode's way:** a Bun process running `@opentui/core` with its Solid reconciler, consuming the core's events over their wire forms and sending commands back; `OpenTUIFrontend` in Python is then only the bridge that owns the process and the stream. It runs the engine version OpenCode runs and needs no binding work; the cost is a second language, with the core's wire schema as the one contract between them.

## Target

- **`OpenTUIFrontend`** by the chosen route, registering views for the core's state families. Start with the transcript, the view the route choice measured, then the sidebars, the goal display, the prompt and the terminal.
- OpenTUI's signals bind directly to `CoreEventStream` subscriptions; its components render view state and send commands.
- **The views are OpenTUI-native:** nothing ports a Textual widget's structure. Where OpenTUI lacks a component Toad needs, the fork grows it.

## Gate

The frontend is usable by you only when it passes every U7 journey that Textual passes, with DU1's bar held across the whole journey suite, not only the transcript.

## Done when

Both frontends pass the same journeys, and you choose which to run.
