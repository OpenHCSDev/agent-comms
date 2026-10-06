# U7: Journeys against the core

**Index:** [README.md](README.md). **Throughout.**

## Why

The usability regressions of the last two days were state regressions: tab state, delivery feedback after a cancel, inbound chronology, launch admission. They live in the core, so they are caught in the core, once, for every frontend.

## Target

- **Each journey is written against intents and state:** send commands, then assert the core's state and events (the turn settled, the reply appeared in order, the tab's state survived a warm return).
- **Each frontend adds a rendering check per journey:** the state is drawn, captured with the TUI recording tools, and reviewable.
- **Latency budgets per journey,** measured in each frontend.
- A refactor leaves every journey identical; a change to a journey is a bug in the refactor.

## Done when

The flows the recent fixes touched each have a core journey with a latency budget, required on any PR touching the files they run through.
