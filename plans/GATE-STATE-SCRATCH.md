# Gate state architecture — scratch

Iterate here: write, rethink, rewrite. Not a final plan. Final plan gets compressed from this.

## Take 1 — what's true for real

- The gate doesn't run a checklist of requirements in sequence. The world is in one
  state right now, and the state answers the call. `if unread / if refusal / if pending`
  is a set of states spelled out as conditionals inside methods. A set of states is
  subclasses of an ABC. Each subclass carries its own properties (the paths, the rule)
  and its own behavior (its refusal words, what it means). Parent ABC declares the
  interface, subclass holds the case. That's it.

- `RuleDeal` + `DealState` (NoDeal/DealRecord) is two hierarchies for one fact.
  The deal's files ARE its state — record, read log, audit. Deal inherits State, so
  deal IS-A state; there is no "deal state" object hanging off it. NoDeal/Dealt stay
  only if they still distinguish something real. MI is the tool: compose `Deal(State)`,
  compose `ToolGateState(State)`, compose more capabilities by inheriting more.

- The two requirement parents are byte-same skeletons:

      def blocked(self, deal):
          if <my condition>: return GateRefusal(<label>, <words>)
          return super().blocked(deal)

  The skeleton is a duplicated fact. It gets stated once, in the parent, as a private
  concrete method. Each requirement contributes ONLY its state class — collected in
  MRO order by `__init_subclass__` (same move as `DeckReader.audiences`).

- Existing mechanisms that already prove each piece, no new machinery needed:
  - `GateAnswer`/`GateOpen`/`GateRefusal` — a tiny state family already exists;
    blocked/open are states, they just don't say so.
  - `turn_inputs.py:93` — "an inbox item performs one operation, not every matching
    base handler": `@handles` dispatch on the state type, one handler per case.
  - `ToolMeaning.of(tool)` (read_gate) — DeclaredFamily member lookup by key.
  - `DeckReader.__init_subclass__` — parents collect contributions in MRO order.

## Take 2 — families; one direction of knowledge

Rule: **consumers hold the family root. Subclasses are declaration only.** Subclass
names appear exactly in three places — their own declaration, the family's own
derivation/dispatch, and `@handles` registrations — nowhere else. Any method that
names a concrete state from outside the family is slop.

- `State` — the one root. MI, not a second parallel ABC: `ToolGateState(State)`,
  `DealState(State)`, and the deal itself inherits `State` too — there is no
  "deal" beside its "deal state". One object, composed with State by inheriting it.

- Deal side: the deal answers ONE query — `state(read) -> DealState`. Declaration-only
  cases: `Undealt` (nothing owed), `Unread(paths)`, `Unengaged(rule, source)`,
  `Unaudited(refusal_text)`, `Answered` (open). Each case carries its properties and
  its own behavior — the refusal words ARE the instance, not text assembled elsewhere.

- Gate side: `ToolGateState(State)` with exactly `Open` and `Blocked(cause: DealState)`.
  Blocked is ONE subclass, tagged with the deal-side cause. Behavior:
  `enforce(gate, tool)` — Open runs the call, Blocked refuses using the cause's own
  interface. The gate (all backends, all hooks) knows only `ToolGateState`:
  `judge_tool` = answer the call with `self.state().enforce(...)`.

- The requirement parents (`ReadsDealtSources`, `AuditsDealtRules`, the new
  engagement) become declaration-only contributions: they register their deriver in
  MRO order via `__init_subclass__` (the `DeckReader.audiences` move). The walk —
  "first contribution whose deriver yields a state, that's the answer" — is ONE
  private concrete method on the base. The `if mine: return GateRefusal(...);
  return super().blocked(...)` skeleton is deleted, not refactored: refusal text
  lives on states, order lives in the collected tuple, body lives once.

- Dispatch where polymorphic call isn't enough: `@handles` on the family
  (turn_inputs precedent — caller hands over an instance, is blind to which).

## Take 3 — the three opens, answered

**1. Order.** Requirement parents collect, via `__init_subclass__`, a tuple of derivers
in MRO order: `(Reads → Unread, Engages → Unengaged, Audits → Unaudited)`. One private
concrete walk on the base: first deriver yielding a state wins; none yields → `Open`.
No record → every deriver yields None → Open (today's `NoDeal → GateOpen`, kept).
The walk's body is stated once — the parents declare, never branch.

**2. `before` dies as a separate concern.** `GateRefusal(before, reason)` splits: the
label and the words move ONTO the blocking cases — `Unread.before = "reading"` with
its own paths text, `Unengaged.before` names the quote owed, `Unaudited` carries the
refusal from `AuditDue`. Gate-side becomes `Blocked(cause)` — ONE subclass of
`ToolGateState`, tagged with the cause, and `enforce` reads `cause.before` /
`cause.reason` through the family interface. `GateAnswer`/`GateOpen`/`GateRefusal`
map to `ToolGateState`/`Open`/`Blocked`; the old three-class family is replaced, not
extended (no compatibility spelling).

**3. Record vs state.** The FieldCodec-decoded record is persistence DATA, not a state:
`(dealt, due)` sits in `.deal.json`, decoded once. State is derived from
record + read log + audit — one walk, one instance. So the current split where
`DealRecord` is simultaneously the encoded record AND the `DealState` case, with
`NoDeal` as its twin, collapses: record keeps the data role, `NoDeal`/case-DealRecord
die as states, and lifecycle moves onto the derived cases — `Undealt.started(deal)` →
`deal.redeal()`, others no-op; `context_observed` likewise. Deal itself inherits
`State` and owns `state()` — no companion `DealState` ABC sitting beside it.
`RuleDeal` + `DealState` two-hierarchy slop is gone; one hierarchy, MI at the
capability points (gate backends compose requirement parents; they never see cases).

**Read staging (the interstitial made structural).** Each blocking case carries what
the current step permits: `Unread(next, remaining, read)` permits the next file in
deal order plus re-reads of `read`; `Unengaged(rule, source, read)` permits re-reads
only — the quote owed gates the next file; `Unaudited`/`Answered` permit every dealt
read. `judge_read` keeps its "way through" role but asks the same family:
path not dealt → full answer; path dealt → `state().permits_read(path)`, else full
answer. Order enforcement is no longer a rule stated beside the states — it's what
`Unread` means.

## What's left to do

1. **Take 4 — the engagement joins the family.** `quote`/`does`/`why` schema on
   `RuleAudit` (FieldCodec-declared, old audit files must still decode), the
   `Unengaged` case carries the owed quote and its source path, an Engages
   requirement parent contributes the deriver in MRO order, and the refusal/reminder
   renders the worked spine.md example. Papers carry no rule name, so they unlock
   with reads — no special case.
2. **Probe the live classes before writing code.** The venv is broken
   (`metaclass_registry` missing, needs PYTHONPATH=src or an editable install —
   install from git+https://github.com/OpenHCSDev/metaclass-registry, do not recreate
   the venv). Then confirm in the interpreter: `DealtRuleGate.__mro__`, the
   MroDispatch/`@handles` source, `DeclaredFamily.member_or_default`, FieldCodec
   round-trip of an old `{rule, broke}` audit against the extended schema, and the
   rule name ↔ source path derivation (name = stem).
3. **Compress this scratch into the real plan.** Fold the state architecture into
   GATE-ENGAGEMENT-PLAN.md (or replace it), delete the scratch.
4. **Implement in `rule_deal.py`.** State root; requirement parents declare derivers
   only; one private walk; `GateAnswer`/`GateOpen`/`GateRefusal` replaced by
   `ToolGateState`/`Open`/`Blocked(cause)`; record collapses to persistence data;
   lifecycle moves onto the cases; `permits_read` staging. Then check the backends
   (`read_gate.py`, `pi_rule_gate.py` judge calls) for any reference to the old
   answer family — they must see only `ToolGateState`. `refuse()` and the deal's own
   codec stay untouched.
5. **Tests.** `answer_deal` in tests/conftest.py writes real verbatim quotes; add
   the eight cases from GATE-ENGAGEMENT-PLAN.md; run lint, typecheck and the gate
   suite (`tests/test_commander_skill_hook.py`, `tests/test_queue_bypass_hook.py`).
