# S5: The turn context, owned and inspectable

**Package:** `docs/refactor/retained-task-memory/`. **Rules:** this package's `00-RULES.md`, which adds to round 2's. **Builds on:** S2's retained classes. **Head audited:** agent-comms `main` at the time of writing; re-verify before editing.

## What is wrong

What an agent sees at each turn has no owner, so nobody can look at it.

- **The coordination preamble is one f-string.** `owned_turn.prepare_prompt` concatenates the thread's identity, behavioural rules about comms tools, the project directory, peer state as `json.dumps` of raw presence dicts (built from string keys and truncated to 50), and, under a goal, the goal's text, progress and instructions, into one string, `self.task`.
- **Other contributors return more strings:** `message_bus.awareness_prompt(owner)`, `goal_states` members' `instruction()`, `input_attempt.historical_notice`, `historical_native_inputs`, `image_inputs.prompt_images`, and the ACP prompt path in `acp.py`. Each appends text; none records where its text came from.
- **Agent guidance has two homes:** `.pi/APPEND_SYSTEM.md`, and the rules written as Python string literals inside `prepare_prompt` ("never sleep or poll", "do not echo acknowledgments", how to use `comms_set_goal`).
- **The only inspection command is `compaction-status`.** Nothing shows what an agent will see next turn, what it saw at an earlier turn, or why.

Nothing in it is hidden: pi's compaction entries keep their summary as plain text (`CompactionEntry.summary`), and every other layer is a file or a wire record. The context exists only as the side effect of many contributors.

## Target

```python
class ContextSegment(DeclaredFamily, affix="Segment"):
    """One part of what an agent sees, with where it came from."""
    def text(self) -> str: ...
    def provenance(self) -> Provenance: ...        # file path and revision, wire message, journal entry, or owner record
    def tokens(self, counter: TokenCounter) -> int: ...

class SystemLayerSegment(ContextSegment): ...      # pi's base prompt, APPEND_SYSTEM.md, AGENTS.md, a skill: path and revision
class CoordinationSegment(ContextSegment): ...     # identity, project, peer state: derived from their owners at assembly
class GoalSegment(ContextSegment): ...             # the active goal's text, progress and the goal state's instruction
class RetainedSegment(ContextSegment): ...         # S2's constraints, decisions and forced facts, with author and supersession
class CompactionSummarySegment(ContextSegment): ...# the summary, with the transcript range it replaced
class TranscriptSegment(ContextSegment): ...       # the live transcript since the last compaction, by entry range
class InjectionSegment(ContextSegment): ...        # deliveries, wakes, awareness and historical notices, by wire reference

@dataclass(frozen=True)
class TurnContext:
    thread: ThreadIncarnation
    turn: TurnIdentity
    segments: tuple[ContextSegment, ...]

    def render(self) -> RenderedInput: ...         # what the provider receives, and nothing else
    def manifest(self) -> ContextManifest: ...     # segment kinds, provenance and token counts; no text
```

- **One assembler.** `prepare_prompt` builds a `TurnContext`; every module listed above contributes segments through it instead of appending strings. Peer state is a typed record derived from presence, not raw dicts.
- **Agent guidance has one home.** The rules now written as string literals in `prepare_prompt` move into the declared instruction files that `SystemLayerSegment` reads, so they are versioned, visible and editable in one place.
- **Inspection:** `agent-comms context <thread>` renders the next turn's context segment by segment, with kinds, provenance and token counts; `--turn <n>` shows an earlier turn from its manifest; `--diff` shows what changed since the previous turn. Toad renders the same structure later, as a view under the headless core.
- **A manifest per turn on the wire:** segment kinds, provenance references and token counts, never the text, so "why did the agent do that?" is answered by what was in its context at that turn.
- **Modular operations,** each an owned command with an author: pin something as a retained constraint, supersede one, drop a segment from future turns, and export segments (for example, every constraint Tristan authored) into `AGENTS.md`, `APPEND_SYSTEM.md` or another thread's context.

## Phases

1. **Now, in parallel with S2:** the segment family for everything that exists today (system layers, coordination, goal, compaction summary, transcript, injections), the single assembler, the manifests and the inspection command. **This phase changes nothing an agent receives:** on recorded turns, the rendered input is byte-identical to today's prompt, checked in the same PR and the comparison deleted afterwards. The guidance literals move out of `prepare_prompt` in the same phase, and their rendered text stays identical.
2. **After S2's retained classes land:** `RetainedSegment`, and the pin, supersede, drop and export operations.

## Guards

- No string concatenation into an agent's input outside `TurnContext.render`.
- No agent guidance as a string literal in Python.
- Every segment has provenance; a segment without one is a type error.

## Journeys and verification

Phase 1 runs the live path on Tristan's saved session with his configured provider and shows the agent's behaviour unchanged, alongside the byte-identical comparison. Phase 2's operations each get a journey: pin a constraint, compact, and see it survive verbatim; export a constraint and see a fresh thread receive it.

## Done when

`agent-comms context <thread>` shows any thread's next-turn context with provenance; every turn writes a manifest; no module outside the assembler adds text to an agent's input; and a constraint exported from one thread reaches a fresh thread intact.
