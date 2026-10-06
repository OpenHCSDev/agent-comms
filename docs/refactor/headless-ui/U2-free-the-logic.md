# U2: Free the indirectly tied logic

**Index:** [README.md](README.md). **After U1.**

## What is wrong

Sixty modules (7,530 code lines) never import Textual themselves but are tied to it through other Toad modules. The ties are few:

| Tied through | Modules depending on it | What it gives them |
|---|---|---|
| `app` | 11 | the application object, reached for state and services |
| `widgets.conversation` | 6 | conversation state held on the widget |
| `session_tracker` | 6 | Textual's `Signal`: gone after U1 |
| `agent_schema`, `agent` | 4 each | agent definitions, plus two Textual messages in `agent` |
| `setting_choices` | 3 | Textual's built-in theme list |

Cut these and `agent_controller`, `agent_session`, `transcript_preparation`, `conversation_kind`, `work_preparation`, `preferences` and `cli` become headless.

## Target

- Logic receives the services and state it needs from the core, never from `app` or a widget.
- **Themes are a frontend concern:** `setting_choices`' theme list moves to the Textual frontend, and the core's settings (T1's tree) hold a theme *choice* each frontend interprets.
- A guard makes the boundary permanent: the core package's import graph contains no Textual module, directly or transitively.

## Done when

The sixty modules import no UI module, transitively, and the guard runs in CI.
