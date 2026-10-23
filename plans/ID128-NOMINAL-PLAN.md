# agent-comms: ids are nominal types

Audited head: `dfc7d8976` (main, 2026-10-23). Every count below is `git grep -cE` over `src/` at that commit; every FieldCodec claim was run against that tree's `field_codec.py` in a scratch interpreter.

## The thesis

An id is a nominal value type, decoded once by FieldCodec where it enters: a store row, a file, the wire, a CLI argument. Every field and parameter carries that type instead of `str`. Then a repeated decode has nothing left to check, so it cannot be written, and the constructor checks that exist only because the field is `str` delete themselves.

## Why this is slop today

The fact "this text is a valid 128-bit id" is declared once, in `Id128Text` (`field_codec.py:1430`). The field holding the id says `str`, so every consumer that wants the fact restates it (BOUND-1):

- 15 `Id128Text.decode(` / `Id128Text.from_text(` calls in 14 modules re-check values that were already decoded where they entered. Five are constructor checks on the record's own field: `acp_extension.py:305` and `:681`, `input_attempt.py:375`, `maintenance_barrier.py:316`, `selected_write_plan.py:89`. Those five are the only validation their records have, because `maintenance_barrier.py:305` and `selected_write_plan.py:71` declare the field as plain `str`, and `FieldCodec.construct` (`field_codec.py:1739-1742`) calls the constructor on every decode. Delete the check without changing the type and the stored row stops being validated at all.
- 15 fields spell the pairing at the field: `Annotated[str, Id128Text]` 14 times in 6 modules, `Annotated[str, MessageIdText]` once at `commander/order.py:784`. A second spelling of a fact the value's type should own.
- 23 hand-written `uuid4().hex` mints in 19 modules beside `Id128Text.mint` (`goal_actions.py:482` mints every goal id this way). One concept, two encodings (IDEN-3).
- On 2026-10-23 Reviewer and MI deleted 16 of these re-decodes by hand in #2936 and #2937, citing each caller's boundary line. That is the right result by the wrong mechanism: nothing stops the next one being written, because the type still says `str`.

## What FieldCodec does today, measured

These facts decide the plan's order:

1. **The wire form does not change.** A `str` subclass encodes to the same JSON string and binds to SQLite as `text`. The schema of a bare `Id128` field equals that of `Annotated[str, Id128Text]` (`{"type": "string", "pattern": "^(?:[0-9a-f]{32})$"}`). No stored data moves, and no carry step is needed. One cost: `typed_table.py:2076-2083` digests the whole owning class's source, so each record class an edit touches rebuilds its table once at the next reconcile. That is a rebuild, not a carry.
2. **The plain `str` codec is exact-type.** `_ExactScalarDecoder.accepts` (`field_codec.py:2180-2181`) is `type(data) is self.target`. A `str` field holding an `Id128` refuses to encode (`TypeError: Expected <class 'str'>, received Id128`), and `FieldCodec.decode(str, an_id)` refuses too. That is the guard we want at the end: an id in a plain-`str` field becomes a runtime error. During the migration it would break every unconverted field the moment the mints start returning `Id128`. So phase 1 adds a narrow allowance: `_TextDecoder` accepts a `NominalText` and encodes it as exact `str`. The last phase deletes it.
3. **The value type cannot be its own representation.** `class Id128(str, Id128Text)` resolves `Id128.encode` to `str.encode` and encodes to bytes. The other base order shadows `str.encode` on every id, and the tree calls `.encode()` on id text (`native_session_files.py:234`). So the value type and its spelling stay two classes, and the spelling declares the value type once, as its generic argument.

## How to execute without churn

- **No stored data changes** (fact 1). Each phase is one PR through the normal batch.
- **Kinds go in order, one PR each.** An id kind is one set of field names that carry one kind of value. Its fields, parameters, mints and re-decodes change together, so the value never crosses into an unconverted field of its own kind. The kinds run smallest first, by the counts below. They share modules (`acp_extension.py` holds both input and root ids), so they run in series, not in parallel.
- **Mechanical rewrites go through one codemod per kind.** The annotation edits are all one shape. Re-decode deletions are by hand, each citing the boundary line where the value entered.
- **Delete, never wrap.** No helper, no alias, no `cast`, no `try`. A site whose caller passes raw text is a boundary: it decodes once with the kind's type and stays.
- **Kinds stay one type.** Every 128-bit kind uses `Id128`. Distinct per-kind types (`InputId`, `TurnId`) would also make passing a turn id as an input id a type error. That is a new distinction, not the deletion of a restated fact, so this plan leaves it out; Tristan decides it separately.

## Phases

Each phase, its predecessors and its evidence on `origin/main` are declared in `src/agent_comms/plan_phases.py` (`Id128NominalPlan`). Counts are at `dfc7d8976`; each check is `git grep -cE PATTERN -- src`, and done is the count shown.

| Phase | Done when, on main | Now | Done |
|---|---|---|---|
| 1 `NominalIdTypePhase` | `NominalText`, `NominalSpelling`, `Id128`, `MessageId` declared in `field_codec.py`; `def mint\(cls(, text: str)?\) -> str:` absent there | 2 | 0 |
| 2 `PlainStrIdRatchetPhase` | `^class PlainStrIdAnnotation\(` in `tools/debt_ratchet.py` | 0 | 1 |
| 3 `ClaimIdsPhase` | `\b(nonce\|operation_id\|generation): (str\b\|Annotated\[str)` | 35 in 10 modules | 0 |
| | `Id128Text\.decode\(self\.(nonce\|operation_id)\)` | 2 | 0 |
| 4 `MessageIdsPhase` | `\b(message_id\|source_message_id): (str\b\|Annotated\[str)` | 37 in 21 | 0 |
| | `Annotated\[str, *MessageIdText\]` | 1 | 0 |
| 5 `TurnIdsPhase` | `\b(turn_id\|finished_turn_id\|report_turn_id\|current_turn_id): (str\b\|Annotated\[str)` | 71 in 24 | 0 |
| | `Id128Text\.(decode\|from_text)\((self\.)?turn_id\)` | 2 | 0 |
| 6 `GoalIdsPhase` | `\b(goal_id\|original_goal_id): (str\b\|Annotated\[str)` | 85 in 24 | 0 |
| 7 `RootIdsPhase` | `\b(wire_root_id\|root_id\|lookup\|recipient_lookup): (str\b\|Annotated\[str)` | 127 in 45 | 0 |
| | `Id128Text\.decode\((self\.)?(owner\.)?(wire_root_id\|root_id\|lookup)\)` | 4 | 0 |
| | `FieldCodec\.decode\(Annotated\[str, *Id128Text\]` | 2 | 0 |
| 8 `InputIdsPhase` | `\b(input_id\|accepted_input_id\|native_id): (str\b\|Annotated\[str)` | 129 in 39 | 0 |
| | `Id128Text\.(decode\|from_text)\((self\.)?(input_id\|native_id)\)` | 6 | 0 |
| 9 `RedecodesDeletedPhase` | `Id128Text\.(decode\|from_text)\(` | 15 in 14 | 0 |
| | `Annotated\[str, *(Id128Text\|MessageIdText)\]` | 15 in 7 | 0 |
| | `uuid4\(\)\.hex([^\[]\|$)` outside `field_codec.py` | 23 in 19 | 0 |
| | `isinstance\(\w+, NominalText\)` in `field_codec.py` (the phase 1 allowance) | 0, 1 after phase 1 | 0 |

The kind patterns count fields and parameters together, so each kind phase covers both.

### 1. The type (`field_codec.py` only)

- Declare `class NominalText(str)` with `__slots__ = ()`: text whose type is its identity.
- Declare `class NominalSpelling[V: NominalText](SpelledText)`. Its `__init_subclass__` reads `V` from the subscripted base with `types.get_original_bases` (as `cli_bridges/monitor.py:258` does) and records the pair once, value type to spelling. `from_text` returns `V(super().from_text(value))`. `encode` returns the validated text as exact `str` (fact 2).
- Declare `class Id128(NominalText)` with `mint() -> Id128`, holding the `uuid4().hex` now at `field_codec.py:1435-1437`. Declare `class MessageId(NominalText)` with `mint(text) -> MessageId`, holding the hash now at `:1448-1450`.
- `Id128Text` (`:1430`) and `MessageIdText` (`:1440`) become `NominalSpelling[Id128]` and `NominalSpelling[MessageId]`. Their `mint`s are deleted; callers use `Id128.mint()` / `MessageId.mint(text)`. That is 15 `Id128Text.mint(` sites in 11 modules and 1 at `messages.py:952`, edited in this phase.
- `FieldCodec._representation` (`:1534`) resolves a bare `NominalText` subclass to its recorded spelling, beside the `WireValue` branch at `:1556`. A field is then declared `input_id: Id128`.
- `_TextDecoder` (`:2203`) accepts a `NominalText` and encodes it as exact `str`. This is the migration allowance, and phase 9 deletes it.
- Wire, schema and stored rows stay byte-identical (fact 1). The test is a round trip of a record with an `Id128` field, an `Annotated[str, Id128Text]` field and a plain `str` field holding an `Id128`.

### 2. The ratchet

`PlainStrIdAnnotation(PerFileOccurrenceMeasure)` in `tools/debt_ratchet.py`, beside `StringFieldMetadata` (`:406`). It counts `str` annotations, on fields and on parameters, whose name ends in `_id`, `nonce`, `lookup` or `generation`. It blocks growth per file, so no new id can be declared `str` while the kinds migrate. 283 such annotations in 107 modules today. The count includes external ids (`session_id`, `tool_call_id`) that this plan does not convert; the ratchet only stops the count from growing.

### 3-8. One id kind per phase, smallest first

For each kind: retype its fields and parameters to `Id128` (`MessageId` in phase 4) and its `Optional`s to `Id128 | None`. Delete its re-decodes and constructor checks, citing the boundary line each value already passed. Route its hand-written `uuid4().hex` mints through `Id128.mint()`. A parameter fed by raw text (a CLI argument, a file name, a foreign record) is a boundary: it decodes once there and nowhere downstream.

- **3 Claims** (`maintenance_barrier.py`, `selected_write_plan.py`, `envelope_claim_transitions.py`, `todos.py` generations, `publisher.py`). These are Installer's two boundaries, where the constructor check is the only validation (`maintenance_barrier.py:305,316`; `selected_write_plan.py:71,89`).
- **4 Messages.** `commander/order.py:784`, `messages.py:952`, and the 37 `message_id` annotations.
- **5 Turns.** Includes `diagnostics.py:143`, `goal_failure_observation.py:76` and `owned_turn.py:135`. A `turn_id` that holds a foreign agent's turn id is not an `Id128`: it is declared a boundary in the PR and the check is corrected with `order check`, never forced.
- **6 Goals.** Includes `goal_actions.py:364,482,686` and `goal_attempts.py:873`, which mint by hand today.
- **7 Bus roots and lookups.** Includes `acp_extension.py:305`, `input_origin.py:104,122,137`, `bus_source_page.py:30,50`, `envelope_claim_transitions.py:209,214`, `proven_source_coverage.py:215-216`, `native_session_files.py:236` and `private_registry_guard.py:289`.
- **8 Inputs.** Includes `acp_extension.py:675,681`, `input_attempt.py:375`, `native_entries.py:567`, `native_pi.py:341`, `native_prompt_binding.py:123`, `native_input_record.py:396` and `pi_payloads.py:1595`.

### 9. Close

Delete what is left: `context_store.py:46` (`Id128Text.from_text(self.value)`), the remaining hand-written mints (temporary file names included: random 128-bit names come from `Id128.mint()`), and the phase 1 allowance in `_TextDecoder`. After this an id in a plain-`str` field fails at its first encode, and the ratchet refuses a new `str` id at review. Neither holds today.

## Crossing

- Open PRs at `dfc7d8976`: #2938 (commander heartbeat), #2928 (installer upgrade), #2914 draft (`input_origin.py`, `owned_turn.py`, `runtime_requests.py` among 17 files), #2469 draft (touches `field_codec.py`, `todos.py`, `goal_actions.py`, `external_transcript.py`). Phase 1 edits `field_codec.py`, so if #2469 is revived it merges main. Phases 5, 7 and 8 touch #2914's files: that draft lands or is rebased first, and no phase edits files of an open PR.
- The remaining-slop plan's 1.1 (field directives as classes) also edits field annotations. Where both touch one field, this plan's type edit and 1.1's directive edit are one line: whoever lands second merges, and neither waits.
