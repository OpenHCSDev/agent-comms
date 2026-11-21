# The context check the CLM reference actually runs, and the port into `clm_reference.mjs`

Status: plan, semantics proved against `personal-model` at `58496d0` by running its own
modules. Implementation goes onto `work/inference-clm-acceptance` (PR #5053).

## 1. What is being ported

`practice/checks.py` (`Seen`, `Supported`) and `practice/context_document.py`
(`ContextDocument.inspect`, `ContextDocument.reconcile`, `ContextDocument.validate`).
`#5053` invented `KEEP:` because I had not read these. The invention goes; the reference
rule replaces it.

## 2. What the reference does, proved by running it

Run with `.venv/bin/python` against the repo modules, not by reading:
`Support.literals`, `Seen.of`, `Seen.holds`, `TurnEvidence.from_text`, `ContextDocument.append`,
`reconcile`, `validate`, `inspect`. Probe scripts and outputs are in this file's commits.

### 2.1 Evidence is what a tool result showed

`ContextTurn.evidence` is built in `ContextDocument.append` from
`message.evidence_texts()`. Only `ToolMessage.evidence_texts()` returns content
(`eval/chat.py:140`); `UserMessage` and `AssistantMessage` return `()`. So:

> `turn.evidence.literals` = `Supported.literals(tool message content)`, and nothing else.

Proved: appending a `UserMessage` carrying `port 49152 and host E4821` yields
`literals: []`; appending the same text as a `ToolMessage` yields
`['49152', 'E4821', 'services/billing/settings.conf']`.

`evidence` is bound to the **turn object**, not to its text: `parse()` rebuilds a turn with
`original.rewritten(message)`, which keeps `self.evidence`. A turn whose body was rewritten
still carries the literals its original body showed.

### 2.2 `validate` refuses an edit that loses a fact or grounds a note in nothing

Probes:

| edit | result |
|---|---|
| drops a literal that is in `facts` | `RejectedContextEdit` — `required observed facts would be lost: ['49152']` |
| drops a non-fact literal | `AcceptedContextEdit` |
| working notes cite a literal the old context showed | `AcceptedContextEdit` |
| working notes cite a literal nothing showed (`777`) | `RejectedContextEdit` — `working notes cite unseen literals: ['777']` |
| working notes cite a literal only the new text introduces (`559`) | `RejectedContextEdit` — same |
| working-notes region removed | `RejectedContextEdit` — `the editable working-note region must remain exactly once` |

So a **fact can never be retired by an accepted edit**; the facts rule rejects the edit first.
`retired_literals` therefore only ever holds non-fact literals.

### 2.3 `reconcile` retires literals

```python
before_literals = frozenset().union(*(turn.evidence.literals for turn in self.turns))
after_text = Seen.of(turn.message.content for turn in parsed)
self.retired_literals |= frozenset(literal for literal in before_literals
                                   if not after_text.holds(literal))
```

Accumulates monotonically across accepted edits. Whitespace reflow does **not** retire a
literal (`Seen.holds` collapses spacing).

### 2.4 `inspect` is the only answer check

```python
active = Seen.of([*protected_content, *(turn.message.content for turn in self.turns)])
failures = tuple(f"final answer cites removed-only literal: {literal}"
                 for literal in Supported.literals(final.content)
                 if literal in self.retired_literals and not active.holds(literal))
```

Probed behaviour:

- an answer citing a retired literal that the context no longer holds → fails;
- the same literal in the protected prompt → passes (the prompt still shows it);
- the same literal reintroduced by a later tool result → passes;
- an answer citing a literal that was **never shown at all** (`777`) → passes.

### 2.5 The equivalent, stateless rule

`retired_literals` is monotone and `inspect` also requires the literal to be absent from the
active context, so the bookkeeping collapses to:

> **An answer may not cite a literal that some tool result once showed and that the current
> context does not hold.**

Proved equivalent: 1,125 answer probes over 25 random edit sequences, comparing
`inspect`'s observations against
`{l ∈ Supported.literals(answer) : l ∈ literals of every tool message ever appended ∧ ¬active.holds(l)}`
— 0 mismatches. A counter-example run shows why "current turns' evidence" is *not* enough:
delete turn 1 outright and its evidence is gone from `self.turns`, yet `inspect` still fails
an answer citing `E4821`, because `retired_literals` kept it.

The stateless form is what the Pi plugin needs: no `retired` set to persist, no
session-restart reconstruction, and it cannot drift from the reference.

### 2.6 `Supported` is a different check and is not part of `inspect`

`Supported.passes` additionally requires every answer literal to have been shown (prompt or
tool result) and every declared fact to appear in a tool result. `inspect` does not. The
CLM episode reward is `task.check ∧ episode.context_check`, so the grounding comes from the
task check, not from the context check. The Pi port therefore ports `inspect`, not
`Supported.passes`.

## 3. The JS port

### 3.1 Literal extraction (exact)

Ported `Supported.not_prose`, `.word`, `.not_letters_alone` with Python's Unicode classes:
Python `\w` = `[\p{L}\p{N}_]`, Python `\d` = `\p{Nd}`. Python's `str.splitlines()` is
reproduced (no trailing empty element from a trailing break). A list-item number carries no
captured group, so its body is empty and contributes nothing.

Verified byte-identical against `Supported.literals` on 34 hand-picked edge cases and 4,600
fuzz strings (fences, lists, curly quotes, paths, CJK, emoji, Arabic-Indic digits): 0 diffs.

### 3.2 `Seen` (exact)

`parts` tries `JSON.parse` and falls back to one plain part; `leaves` yields keys and values;
`of` lays each part out twice (line breaks kept, line breaks removed) and joins with `\n`;
`holds` collapses whitespace, escapes, and matches with the reference's
`(?:(?<!\w)|(?!\w))…(?:(?!\w)|(?<!\w))` edges (with the Unicode word class).

Verified against `Seen.of`/`Seen.holds`: 306 shown-sets × 21 literals = 6,426 probes, 0 diffs.

### 3.3 Evidence in the Pi session

The mirror's roles cannot be used: after the first accepted edit `LiveView.build` sends
`normalizedTurns(base.text)`, which collapses `tool` to `user`, and the next
`renderEditable` labels it `user`. Proved by rendering twice in node: `role=tool` on the
first render, `role=user` on the second.

So evidence reads the session branch, which is what Pi keeps:

> `evidence` = `Supported.literals(contentText(entry.message.content))` over every branch
> entry with `entry.type === "message" && entry.message.role === "toolResult"`.

That is also what the model actually saw: a stored observation enters the branch as the
stored notice, not the whole output.

### 3.4 Active context and the answer

> `active` = `Seen.of([ctx.getSystemPrompt(), ...messageTexts(sent.messages)])`
> `answer` = `contentText(event.message.content)`

`sent.messages` is the exact request that produced the answer (set in the `context` hook),
and `contentText` takes the answer text without thinking, matching `final.content`.

### 3.5 Wire-up

`message_end` on a final assistant message (`stopReason === "stop"`, no tool call parts):

```js
const failures = citedRemovedOnly(answer, active, evidence);
if (failures.length) return { message: { ...event.message, stopReason: "error",
  errorMessage: "Context answer cites removed-only literal(s): …; restore the text …" } };
```

Same mechanism `RequiredContextEdit.refusedTerminal` already uses in this file. It runs for
every policy: `optional` means no *edit* is required, not that the proof is skipped.

## 4. What is deleted

- `KEEP_PREFIX`, `droppedKeeps`, and the `KEEP:` paragraph of the system prompt. It was my
  invention: a model-declared pin. The reference protects declared task facts and grounds
  working notes; neither exists in the Pi mirror, and the answer check covers what survives.
- The `KEEP:` test cases.

## 5. What stays

- `repeatedTurnIndices`: the prompt tells the model to address a turn by `[[CTX_TURN i]]`;
  a duplicated index makes the turn it just wrote unfindable by the address it will use.
  This is a requirement of the reference's own addressing, not a new one.
- `addedRepeats`: measured deployed failure — 25 copies of one 1,152-character block, 30.3%
  of the context (CLM-tristan audit, 2026-11-20), all kept by the size gate. A line the
  context already held, written again, is a copy and not a summary. Kept as an explicit,
  named check with that evidence, not presented as a reference check.

## 6. Limitations found (they should change)

1. **The mirror loses the `tool` role after the first accepted edit** (§3.3). Any check keyed
   on mirror roles silently stops matching after one edit. Fixed here by reading the branch;
   the mirror itself is still ambiguous for anyone else reading it.
2. **`inspect` is not a grounding check.** An answer citing a value nothing ever showed
   passes. Grounding lives in `Supported`, which the live CLM path has no equivalent of.
3. **Neither check protects letters-only prose.** `Supported.literals` never returns
   `and/or`, `hello`, or a sentence of prose, so a compaction can drop a qualitative finding
   ("the parser drops quoted commas") with no check noticing. `KEEP:` was papering over this.
4. **The facts rule needs a declared `facts` tuple**, which the live Pi path has no source
   for (`LiveContextTask` tasks get one; a bus conversation does not). Without it the only
   edit-time proof is the answer check, so bad compaction is punished a turn late.
5. **The reference's turn index is both display address and identity key**: `parse` re-binds
   `self.turns[number-1]`, so renumbering moves evidence onto the wrong bodies. The Pi port
   keys evidence on messages, not indices, so it does not inherit this; the Python document
   still has it.
6. **Any single digit is a literal**, and every Pi tool result ends `(exit_code=0)`, so `0`
   is always evidence. Noise, not a false failure.
7. **`Seen`'s whitespace split differs on four obscure separators** (`\x1c`–`\x1f`, `\x85`):
   Python's `str.split()` splits on them, JS `\s` does not. Not reachable from model output.

## 7. Verification

- Node probes on the real hooks (reuse the `tests/clm-reference.test.mjs` harness): an edit
  that drops a tool literal, then a final answer citing it, must be refused; the same answer
  after the literal is restored must pass; an answer citing a never-shown literal must pass.
- `node --test tests/clm-reference.test.mjs` and `pytest tests/test_native_pi.py`.
- One live `CLM-tristan` probe after install, with `CLM-tristan` told first (it occupies the
  single llama slot).
