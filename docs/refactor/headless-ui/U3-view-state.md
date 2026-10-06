# U3: View state moves into the core

**Index:** [README.md](README.md). **After U2, together with T4, T5 and TC1.** Patterns: IDEN-3, IMPL-10.

## What is wrong

136 Textual reactives hold application state on widgets, 28 of them in `Conversation`. Meanwhile, T4, T5 and TC1 are already replacing flags and optional fields with explicit states (`TranscriptState`, `SidebarRow`, `GoalDisplay`, workspace states, the turn binding) and are building them **inside widget modules.**

## Target

- **Those surfaces build their state families in the core.** This amends T4, T5 and TC1: each new state family lives under `toad.core` and is published through `CoreEventStream`; widgets hold a reference to the state they render, never the state itself.
- **Reactives become subscriptions:** a widget's reactive that mirrors application state is replaced by a subscription to the core state it renders. Reactives that are purely presentational (hover, focus, animation) stay on the widget.
- Intents flow the other way as a `Command` family (`SendPrompt`, `CancelTurn`, `OpenThread`, T3's `ThreadAction`), handled by the core, never by a widget.

## State authority (DU2)

Before committing the runtime families, replay a recorded session into the transcript state held both ways, in an `ObjectState` and as a typed family with an event stream, and time the two. Configuration uses `ObjectState` regardless.

## Done when

No reactive on a widget holds application state; T4, T5 and TC1's families live in the core.
