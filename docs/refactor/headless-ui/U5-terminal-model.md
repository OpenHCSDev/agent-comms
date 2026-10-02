# U5: The terminal splits into model and view

**Index:** [README.md](README.md). **After U4.**

## What is wrong

Toad's ANSI engine is already half headless: the stream parser, key and DEC handling import no Textual. But `TerminalState`, the screen buffer the commands mutate (T7), lives beside the widget that draws it.

## Target

- **The screen buffer is core state:** lines, cells, cursor, modes and scrollback, mutated only by T7's commands and published as change events (which lines changed).
- **Each frontend draws it:** Textual as today; OpenTUI through its native text buffers, which is where a fast terminal view would come from.
- T7's performance gate (the large-stream pilot) now measures the model and each view separately.

## Done when

The terminal's model imports no UI module, and the large-stream pilot passes for the model and the Textual view.
