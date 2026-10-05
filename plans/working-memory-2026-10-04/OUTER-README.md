# Working memory: seeing what an agent has been given

**Heads:** agent-comms `main` at `9c297ef4`, Toad fork at `34633149`. **Rules:** each repository's `00-RULES.md`; agent-comms' `.pi/APPEND_SYSTEM.md`.

## The problem

Asked where a rule came from, agents answer "development instructions" and can't say more. The answer is literally the most precise the system can give: pi's whole system prompt is one `SystemLayerSegment`, a single string rendered into `systemPrompt`. Pi's base prompt, `APPEND_SYSTEM.md`, every `AGENTS.md` and the skills arrive concatenated, under one native provenance, and nothing fills that segment's `contributors`. Rules agents invented for themselves survive in compaction summaries and get cited back as instructions, and nothing distinguishes them from Tristan's.

A hosted model's weights don't change between turns, so its context is its entire working memory. Organizing the context, span by span, with each span's source and kind, is a view of that working memory. These plans build it.

## What exists

- agent-comms models context as a `ContextSegment` family with a `Provenance` family (file, owner, wire, journal, native, preview, resource), per-turn manifests with `sha256` and token counts, and span coordinates (`ContextSegment.contribution` returns `InputContributionCoordinates`).
- pi's `TurnContextObserved` becomes agent-comms' `ContextObserved` event every turn.
- Toad's `core/context_inspection.py` holds a `ContextNode` family; `widgets/context_explorer.py` renders it as a tree with search and export.

## Why Jev, and where its authority ends

Jev (TypeSafe AI) takes a state and named typed questions (Choice, Noul, Score) and returns decisions with probabilities, never text ([documentation](https://openrouter.ai/docs/guides/community/jev)). It labels spans without rewriting them, so every item the interface shows is the agent's literal context. Releases can be pinned (`jev-1.13`), which an audit trail needs. Its answers are always in schema, and can still be confidently wrong, so its probabilities are measured on this corpus before anything relies on them (W7).

**Jev never decides provenance.** Which file, which revision, which message, who wrote it: the system knows these by construction, and a classifier deciding them would be a second authority guessing at a fact the system owns. Jev types the meaning of prose; structure supplies everything else.

## Surfaces

| ID | Surface | Repository |
|---|---|---|
| [W1](W1-system-layer-sources.md) | The system layer records its source files | agent-comms |
| [W2](W2-spans-and-questions.md) | Spans, and questions declared as families | agent-comms |
| [W3](W3-classifiers.md) | Classifiers as a family; durable, versioned annotations | agent-comms |
| [W4](W4-disclosure-policy.md) | What may leave the machine | agent-comms |
| [W5](W5-annotation-worker.md) | The harness classifies, never the agent | agent-comms |
| [W6](W6-working-memory-view.md) | The working-memory view, with corrections | Toad |
| [W7](W7-calibration.md) | Calibration and thresholds | agent-comms |
| [W8](W8-rule-sentinel.md) | Rule proposals and claims on the wire | agent-comms |

## Order

1. **W1 first.** Without it, every rule's authority reads "native system prompt", and the view can't answer its main question.
2. **W2, W3, W4 and W5** together, for two span kinds only: rules and commitments.
3. **W6** with the Obeys and Promised sections.
4. **W7,** then the remaining span kinds.
5. **W8** once W7 shows the rule questions are calibrated.

## Decisions

| ID | Question | Default |
|---|---|---|
| **DW1** | Which segment kinds may be sent to a third party? | Instructions, compaction summaries and agent messages; never tool output, file contents or anything resembling a secret (W4) |
| **DW2** | Which route to Jev? | OpenRouter's decisions API with the pinned `typesafe/jev-1.13`: no waitlist, one key, pinned release |
| **DW3** | Which annotations does the view show? | Above the calibrated threshold, plainly; between thresholds, marked uncertain; below, hidden (W7) |
