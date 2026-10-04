# W8: Rule proposals and claims on the wire

**Index:** [README.md](README.md). **After W7 shows the rule questions are calibrated.**

## Why

Agents invent rules and announce them inside PRs and long threads, where they're lost. Asked later, they cite "instructions" that exist nowhere Tristan wrote.

## Target

- **Agent messages on the wire are classified as they arrive,** using the same span model and questions.
- **A proposed rule** becomes a typed rule proposal in Tristan's queue, one line each: author, scope, text, and the rule it would supersede. Until he ratifies it, it binds nobody, its author included.
- **A claimed instruction** ("I was instructed to…") is checked against the sources of the claiming agent's context. A claim matching no source is flagged on the spot.

## Done when

Every rule an agent proposes appears in Tristan's queue as a typed record, and claims of instruction with no matching source are flagged when they're made.
