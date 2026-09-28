# Shared abstractions across the refactoring surfaces

**Round-2 package copy.** [00-RULES.md](00-RULES.md) overrides anything here that conflicts with it: no compatibility, no converters, aggressive deletion, tests only where they protect behaviour.

**Read this before any surface file.** The surface receipts kept finding the same mechanisms: a family base with derived names, a field-derived encoder, a lifecycle-state base, capability dispatch, a command family, request correlation, thread incarnation, a locked store. Specified separately in each surface, parallel agents would build each of them several times, which is the replica problem this audit exists to remove, reproduced at the level of the plan.

So each shared mechanism is defined **once, here**: what it is, what it replaces, which surface builds it and in which wave, and which surfaces reuse it. A surface file says what it builds and uses and links back here; it never re-specifies an abstraction. If a surface needs something an abstraction does not provide, extend the abstraction here, in the building surface, and do not fork it.

| ID | Abstraction | Built by | Wave | Used by |
|---|---|---|---|---|
| [A1](#a1-declaredfamily) | `DeclaredFamily` | shared foundation, carried by S6 | before wave 1 | S2, S3, S4, S6, S7, S8 |
| [A2](#a2-fieldcodec) | `FieldCodec` | shared foundation, carried by S6 | before wave 1 | S2, S3, S4, S6, S7, S8, A8 |
| [A3](#a3-lifecyclestate) | `LifecycleState` | S3 | 1 | S3, S8, S7, and S2 optionally |
| [A4](#a4-mrodispatch) | `MroDispatch` | S1 | 1 | S1, S7, S8, and S2 optionally |
| [A5](#a5-command) | `Command` | S8 (inbound); S2 builds the outbound variant | 3 (and 1) | S2, S7, S8 |
| [A6](#a6-pendingrequests) | `PendingRequests` | S1 and S2 (one agent) | 1 | S1, S2 |
| [A7](#a7-incarnation) | `ThreadIncarnation`, `TurnIdentity` | S5 | 3, first | S1, S2, S3, S4, S7, S8 |
| [A8](#a8-lockedstore) | `LockedStore` | a small wave-2 commit beside C0 | 2 | S4, S5, S7, S8 |
| [A9](#a9-displaybasis) | `DisplayBasis` | S4 | 3 | S4, S5 |
| [A10](#a10-agentevent) | the `AgentEvent` family | S1 | 1 | S2, S7, S8 |
| [A12](#a12-childprocess) | `ChildProcess` | S13 (round 2) | round 2, step 1 | S9, S10, S13 |
| [A13](#a13-typedtable) | `TypedTable` | S12 (round 2) | round 2, step 1 | S9, S10, S12, S7 |
| [A14](#a14-pihelper) | `PiHelper` | S9 (round 2) | round 2, step 3 | S9, S7 |
| [T](#t-testing-patterns) | testing patterns | everyone | all | all |

---

## A1 DeclaredFamily

**What it is.** A base for closed families of classes. Each concrete subclass registers itself under a **name derived from its class name** (snake case), with an optional **family affix** stripped first, so `ChannelScope` in a family with affix `"Scope"` derives `"channel"`. The family rejects name collisions, decodes a stored name back to its class, lists its names, and filters its members by capability.

```python
class DeclaredFamily:
    declared_name: ClassVar[str]      # derived; not `name`, which real fields use

    def __init_subclass__(cls, affix: str | None = None, declared_name: str | None = None, **kw): ...
        # a family root declares affix=; a member passes declared_name= only where an external
        # contract spells it differently from the derivation
    @classmethod
    def decode(cls, name: str) -> type[Self]: ...
    @classmethod
    def names(cls) -> tuple[str, ...]: ...                   # for golden tests (T1)
    @classmethod
    def members_with(cls, capability: type) -> tuple[type[Self], ...]: ...
```

**`members_with` replaces hand-written rosters of members sharing a property.** Found so far: `_SESSION_MUTATING_COMMANDS` (S2), the phase-set roster for stall exemption (S2), the goal-sync roster `('input_started', 'done', 'settled')` (S1), `comms_goal`'s hand-listed choices (S8).

**Rules.**
- Names are never written by hand. Where a family's names are an **external** contract (pi's event kinds, pi's command names), a golden test (T1) pins them. Names used only in our own stores and protocols change freely; the stores are reset at cutover.
- Use an affix when a family's natural short names would collide on import (`declarations.Channel` versus an export scope; S3's `Completed` versus a goal's). Module namespacing is enough only where the classes are never imported side by side.
- One registry per family, so the same name in two families (`direct` in `ResponsePolicy` and `MessageAudience`) never collides.

**Replaces:** hand-written `value = "…"` declarations, kind strings, and enum-member rosters across S2, S3, S4, S6 and S8.
**Built by:** the shared foundation, one small commit before wave 1, carried by S6 as the smallest surface (index, precondition 4).

---

## A2 FieldCodec

**What it is.** Encoding and decoding derived from declared dataclass fields. A field holding an A1 member encodes as its derived name plus its own fields; nested dataclasses recurse; field names are the keys. One codec serves every boundary: sockets, files and the store.

```python
class FieldCodec:
    @staticmethod
    def encode(value) -> dict | list | str | int | float | bool | None: ...
    @staticmethod
    def decode(cls: type[T], data) -> T: ...
```

**Replaces:** field-by-field `to_primitive` and `to_wire` methods, and the parsers that validate the same format on the other side:

- S3: `RecoverySnapshot.to_primitive` (145 lines, 87 keys), the four `Projected*` mirrors, and the gateway client's five replica rosters on the other end of the socket;
- S4: `SavedView.to_wire`, `ViewPredicate.to_wire`, the `Message` mapping;
- S6: scope and limit `to_wire`. Because S6's limit classes keep a field named `value`, the codec reproduces today's export header byte for byte;
- S2: each pi command's `to_rpc`;
- A8: every stored record.

**Rule:** the codec derives, and a golden fixture (T1) pins each external format. Where an external format genuinely spells a key differently from the field, that is declared on the field once, never by hand-writing the whole mapping.
**Built by:** the shared foundation, in the same commit as A1.

---

## A3 LifecycleState

**What it is.** A1 plus lifecycle semantics. Each state is a class carrying **only the data valid in that state**, declares its successors, and owns its behaviour, so illegal combinations cannot be constructed.

```python
class LifecycleState(DeclaredFamily):
    @classmethod
    def successors(cls) -> tuple[type["LifecycleState"], ...]: ...   # a method, for forward references
    def may_become(self, nxt: "LifecycleState") -> bool: ...
    @classmethod
    def transition_table(cls) -> dict[str, frozenset[str]]: ...     # derived, for anything that needs one
```

**Replaces:** value-only enums whose behaviour is switched on externally, hand-written `*_TRANSITIONS` tables, and runtime checks rejecting illegal combinations of enum values.

**Used by:**
- S3: the execution, attempt, claim and obligation lifecycles (about 47 runtime checks become unconstructible states);
- S8: `GoalState` (`PausedGoal(source)`, `BlockedGoal(reason)`) and, within S8's scope, the attempt store's `Ready` and `Reserved`;
- S2, optionally: if the attempt phase becomes the persisted projection of the turn phase (S3's OPEN-1), `TurnPhase` declares successors through A3 and its event-driven transitions are checked against them.

**Built by:** S3, in wave 1, on A1. S8 builds `GoalState` on it in wave 3 and must not create a second lifecycle base.

---

## A4 MroDispatch

**What it is.** Consumers register handlers against classes, including capability classes, and dispatch runs **every handler found along the value's MRO**, most specific first. A handler registered on an intermediate base or a capability therefore applies to every class beneath it.

```python
class MroDispatch:
    """Handlers keyed by class; dispatch walks type(value).__mro__."""
    def register(self, cls: type): ...          # decorator
    async def dispatch(self, value, *args): ...
```

**Replaces:** consumer-side string dispatch and hand rosters of cases that share a reaction. In S1, the goal-sync reaction registers once on `TurnLifecycleEvent`; the correlation reaction once on `SettingChangeResult`; the WORKING-activity reaction once on the consumer base.

**Used by:**
- S1: `AgentEventConsumer`, the base both event consumers extend;
- S7: `_emit_event` (230 lines, branching on the event kind 16 times) becomes an `AcpUpdateConsumer` extending the same base;
- S8: the goal-changed event reaches `acp.py` through it;
- S2, optionally, if phase reactions to pi events benefit from registration by event class.

**Built by:** S1, in wave 1.

---

## A5 Command

**What it is.** A closed command family on A1: each command is decoded once at its boundary by derived name, carries **its own parameters** as fields decoded by A2, and applies itself. Preconditions shared by every command of a family are one object on the base.

```python
class Command(DeclaredFamily):
    @classmethod
    def decode(cls, data: dict) -> "Command": ...     # name via A1, fields via A2
    def apply(self, ctx): ...
```

**Replaces:** a string `action` dispatched inside one long function with a parameter list covering every action. Four instances:

| Instance | Today | Surface |
|---|---|---|
| `GoalAction` | `update_goal`: 296 lines, 15 parameters | S8 |
| `RuntimeRequest` | `runtime.handle`: 205 lines, `action` compared 12 times | S7 |
| `CliCommand` | `cli.main`: 195 lines, `args.command` compared 27 times; the argument parser derives from the family | S7 |
| `PiCommand` (outbound variant) | 8 command types and 8 scattered response checks | S2 |

The outbound variant shares A1 and A2 but, instead of `apply`, builds its payload with A2 and interprets its own response through A6.

**Actor capabilities** (S8's `ModelInvocable`, `OwnerInvocable`, `RuntimeInvocable`) are ordinary capability classes filtered with A1's `members_with`.

**Built by:** S8 builds the inbound base in wave 3; S2 builds the outbound variant in wave 1 on A1 and A2. S7 reuses the inbound base.

---

## A6 PendingRequests

**What it is.** Correlation of a response with the request that produced it, keyed by id, with timeout and cancellation.

**Replaces:** S1's parallel `_model_requests` and `_thinking_requests` maps (ACP side), and S2's eight `kind == "response"` checks (backend side).

**Built by:** the single agent doing S1 and S2 in wave 1: one helper, both boundaries. The two layers stay separate (they are different boundaries); only the mechanism is shared.

---

## A7 Incarnation

**What it is.** Identity values that change only when what they identify changes.

```python
@dataclass(frozen=True)
class ThreadIncarnation:
    name: str
    created_at: float              # a deleted-and-recreated thread is a new incarnation
    owner_generation: int          # bumped only when ownership changes

@dataclass(frozen=True)
class TurnIdentity:
    incarnation: ThreadIncarnation
    turn_generation: int           # bumped on every turn claim, and by nothing else
```

**Replaces:** `owner_epoch`, bumped both on ownership changes and on every turn claim; `_turn_epochs`; and "epoch" and "generation" as synonyms across call boundaries (S5).

**Used by:** S4 (a DM conversation is keyed by its participants' incarnations), S3 (attempt fences, per S5's OPEN-2), S2 (the turn session's identity), S1 (`settle_turn` takes a `TurnIdentity`, as a retrofit after wave 3), S8 (reported turns and failed-turn observation), S7 (`TurnRunner` works in terms of `TurnIdentity`).

**Built by:** S5, as the first thing it lands in wave 3 (its steps 2 and 3), because S4 depends on it.

---

## A8 LockedStore

**What it is.** One typed, locked, atomic store for a document on disk, using A2 for its record type, taking **a shared lock for reads and an exclusive lock for writes**.

```python
class LockedStore(Generic[T]):
    def __init__(self, path: Path, record: type[T]): ...
    def read(self) -> T: ...                                   # shared lock
    def update(self, change: Callable[[T], T]) -> T: ...      # exclusive lock, atomic replace
```

**Replaces:** the same locked read-modify-write hand-rolled in 19 modules: 211 `_store_lock` calls, 39 `_atomic_write_text`, 21 `json.loads(path.read_text())`. It is also the **single place to apply the post-mortem's recommendation** to take shared locks for reads, instead of changing 211 call sites; and it owns each store's filename, retiring `"read_markers.json"` hard-coded nine times (S4) and `"goal_waits.json"` in two modules (S8).

**Not for the wire itself.** The bus is an append-only log with its own fsync and claim-gate rules; it shares A8's lock policy but not its read-modify-write shape.

**Used by:** S4 (`ReadLedger`), S5 (registry counters, where it touches persistence), S8 (goal waits and pause history), S7 (the registry, runtime info, activity, relationships, channels, input dispositions, transcript routes).

**Built by:** a small wave-2 commit beside C0. It is new code rather than relocation, so it lands separately from C0's moves, and before wave 3 needs it.

---

## A9 DisplayBasis

**What it is.** A record of exactly what a display showed a viewer: which conversations, through which messages, between which `ThreadIncarnation`s (A7). It is the **only** thing that may advance read position.

**Replaces:** view-keyed watermarks and captured marker keys (S4), and the DM proof that compares the whole registry file revision and turn-bumped epochs (S5).

**Used by:** S4 (`ReadLedger.mark_displayed`) and S5 (the DM acknowledgement compares the incarnations the basis recorded).
**Built by:** S4 in wave 3, on A7.

---

## A10 AgentEvent

**What it is.** S1's closed family of frozen event classes for what the backend reports during a turn: the shared vocabulary between producers and consumers.

**Used by:** S2 (its turn session produces them), S8 (adds a goal-changed event, so `acp.py` stops inferring goal changes from a tool's name), S7 (`AcpUpdateConsumer`).
**Built by:** S1, in wave 1. Extending the family means adding a class to it; there is never a second event vocabulary.

---

## A12 ChildProcess

**Added in round 2** ([01-INDEX.md](01-INDEX.md)); built on `main` before any user adopts it.

**What it is.** One owner for a child process's life: spawning it, applying a deadline, stopping it gracefully, killing it when the grace period expires, and reaping it, with each outcome reported as a typed value rather than an exit code to interpret.

**Replaces:** the same mechanism implemented in eleven modules at four levels of rigour, from race-free `pidfd` and PID-namespace containment down to bare-PID identity for detached owners (measured at `0c63715`; see S13).

**Used by:** S13 (in `backend.py`, `owner_lifecycle.py`, `recovery_gateway.py`), S10 (in `native_pi.py`, `selected_pi_child_deadline.py`), S9 (in the compaction modules).

**Also owns process identity:** `ProcessIdentity` (PID plus start time), the process-level counterpart of A7's `ThreadIncarnation`, verified before any liveness answer or signal. See [S13](S13-child-supervision.md).
**No bare-PID path.** Liveness and signals always go through a `ProcessIdentity`; there is no code path that acts on a PID alone. Owners launched before A12 are relaunched at cutover.

**Built by:** S13, in round 2's first step, as a new module only, so that it collides with nothing.

---

## A13 TypedTable

**What it is.** One table, one row type, and the row type is the single authority for the table. The row type is a frozen dataclass on A2. From it A13 derives:

- **the DDL:** columns from fields; primary keys, uniqueness, references, `STRICT` and indexes declared as field or class metadata;
- **reads:** each row decoded strictly into the row type;
- **writes:** inserts and updates with column lists derived from the fields.

It also owns SQLite's representation (booleans as integers, nested records as JSON text), converting to and from the declared field types, so A2 stays strict about coercions.

**Replaces:** hand-written DDL restating each class; 321 reads by column name or index across 32 modules over 57 tables; 34 positional `INSERT`s coupled to column order; tables such as `native_runtime_inputs` with no row type and eight raw readers.

**No compatibility.** Deriving the DDL changes schemas; the affected stores are runtime or derived state and are reset at cutover ([00-RULES.md](00-RULES.md), rule 2). There is no code that reads an old schema.

**Used by:** S12 (its tables and `native_runtime_inputs`), S10 (the tool broker's read), S9 (the compaction tables), and every other store as it is touched.
**Built by:** S12, as a new module first, in round 2's step 1.

---

## A14 PiHelper

**Added in round 2** ([S9](S9-compaction.md)).

**What it is.** A JavaScript program shipped with the package as a data file, run against pi's package. Each helper declares only its script and its request and result records; running it under a deadline (A12's `BoundedRun`), encoding its input and strictly decoding its output (A2), and reporting failures are inherited. It is a composition of A12 and A2, named because six places need exactly that composition.

**Replaces:** JavaScript embedded in Python strings (about 10,000 characters across six modules, invisible to every tool), each run through its own `subprocess.run(timeout=…)` and parsed with a hand-written exact key-set check and `type()` checks.

**Used by:** S9 (five helpers), round 1's S7 (`tool_output.py`, if its helper has the same shape).
**Built by:** S9, in round 2's step 3.

---

## T Testing patterns

Tests protect behaviour, not structure ([00-RULES.md](00-RULES.md), rule 5). These are the only patterns a surface should reach for:

- **T1, golden formats, for external contracts only:** pi's RPC stream, pi's settings, ACP. Never pin one of our own formats; that is compatibility by another name.
- **T2, one new-case test per abstraction:** a test-only member (a family member, a helper, a row type) that works with no edit to existing code. One per abstraction, not one per surface or per member.
- **T3, characterize first, only for a known bug:** write the correct behaviour as an expected failure, then make it pass. Not for restructuring.
- **T4, recorded replay, only for external streams:** a handful of real recordings, not an exhaustive corpus.
- **T5, properties, only where an invariant is the point,** such as "a message is read only if it was displayed."

Tests of deleted code are deleted. Tests of internal structure are deleted when the structure changes. One family-level test replaces a test per member.

---

## Build order at a glance

```
before wave 1   A1 DeclaredFamily, A2 FieldCodec        (one commit, carried by S6)
wave 1          A3 (S3) · A4, A6, A10 (S1+S2) · A5 outbound (S2)
wave 2          A8 LockedStore                          (beside C0, separate commit)
wave 3          A7 (S5, first) → A9 (S4) · A5 inbound (S8)
wave 4          S7 reuses everything; builds nothing new
round 2         A12 ChildProcess (S13, step 1) · A13 TypedTable (S12, step 1) · A14 PiHelper (S9, step 3)
```

A surface may only use an abstraction whose builder has landed. If it needs one early, the abstraction moves earlier in this table, never into a second copy.
