# W6: The working-memory view

**Index:** [README.md](README.md). **Repository:** Toad. **After W5.**

## Target

New `ContextNode` members in `core/context_inspection.py`, grouped into sections keyed by span kind:

| Section | Holds |
|---|---|
| Obeys | rules, grouped by authority from provenance: Tristan's files and messages, the harness, agent-written files, compaction summaries only |
| Wants | goals and the active task |
| Believes | claims, marked supported or unsupported by evidence in context |
| Decided | decisions with their rejected alternatives; typed `Decision` records come in natively |
| Promised | commitments, marked fulfilled or open |
| Unsure | open questions and stated uncertainty |

- **Rules found only in compaction summaries get their own group.** That's where "I was instructed" turns out to be the agent's own earlier conclusion.
- **Each item** links to its exact span, and shows its probability and classifier version.
- **A correction action** writes a `HumanLabel` (W3).
- **Typed throughout:** Toad reads annotations through agent-comms' typed API, never dictionaries.
- **First sections:** Obeys and Promised; the others follow their span kinds.

## Done when

The explorer shows Obeys and Promised for any thread, every item opens its span, and a correction changes the label shown.
