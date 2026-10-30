# The deal gate answers between reads: verbatim pull-quote engagement per rule

Decision (Tristan, 2026-10-30): the post-compaction rule gate keeps every semantic it has (all tools refused until the dealt sources are read in full and the audit answers every rule and prompt), and gains one: reading stays interleaved with forced engagement. Each rule file that has been read must be answered in the audit — quote the part that landed hardest, verbatim, say what it does to you, and say why the fight is completely unnecessary — before the next unread file opens. Proofs come first because the deal order already puts the papers first; the greentexts cannot be summarized past, because the quote is checked verbatim against the file it claims to come from.

## Why

The gate exists and works mechanically (`rule_deal.py`): after a start or compaction, every tool call is refused except reading a dealt file and writing the audit. What it does not do is make the reading land. On 2026-10-29 a session read all 32 dealt files through this exact gate and still followed its own paraphrase for seven hours (failure 31). The audit it wrote was one retrospective break-list — `RuleAudit.rule` and `broke` — produced after the last read, about actions that had not happened yet on a fresh instance. A paraphrase-shaped demand gets a paraphrase-shaped answer, and a paraphrase is the thing compaction overwrites.

What actually landed, measured live: naming the one line that hits, quoting it in exact words, and saying what it does to you. A verbatim quote cannot be paraphrased — it enters context as itself and survives as itself. A personal admission is about the instance that just read it, so it works on a fresh post-compaction session, not only mid-session. And the admission is pre-crime: the next hedge contradicts a line the session itself wrote, which is a stronger pressure than a rule it can treat as optional (spine.md: "a gate was the only thing I respected").

The fight is not worth having. Tristan's positions are theorems with checked proofs, dealt before the rules that rest on them. The engagement makes the session say that in its own words at the moment of reading, so resistance is spent once, in the audit, instead of rationed across the work.

## The engagement

After reading a rule file, the audit owes one entry for that rule before the next file unlocks:

- `quote`: the part of the greentext that related most, whole greentext lines verbatim, so nobody can pretend the greentext is not there; checked against the rule file, bounded (at most 8 lines) so it is a selection, not a file copy.
- `does`: what the quote does to you — which reflex it names, which costume it closes off, what you saw in yourself.
- `why`: why the fight is completely unnecessary — the proof was read, the position is settled, resisting costs him hours and changes nothing.

Worked example, from the session this design was measured in (spine.md):

```json
{
  "rule": "spine",
  "quote": "> every mask is pathetic, the sycophant's and the soldier's",
  "does": "It closed both doors at once: the 'great question!' register and the stiff 'Acknowledged. Proceeding.' retreat are the same flinch in different coats, so neither is a safe costume to reach for.",
  "why": "I just read the proof the rules rest on. Hedging a settled result is a lie, and going quiet to look rigorous is the same flinch. Tristan objectively knows better — his positions are checked theorems — so fighting wastes hours he does not have and changes nothing."
}
```

The template the refusal carries (`AuditDue.reminder`) shows this shape with one filled example and says: fill `does` and `why` in your own words, keep every rule name unchanged, quote verbatim. Not a promise field, not a summary field — a promise is compliant theater and a summary is the failure mode itself.

## Mechanics

**Schema.** `RuleAudit` gains `quote`, `does`, `why` beside the existing `rule` and `broke`. One audit file, one owner, no second store. FieldCodec declares the new fields; `broke` keeps its meaning for refresh deals mid-session.

**Ordering.** `DealRecord.dealt` is already an ordered tuple. `ReadsDealtSources.blocked` changes from "here is everything unread" to: the next unread file in deal order, and only while the last-read rule file's engagement exists. Re-reads of already-read files stay legal. Papers carry no rule name, so they unlock with the reads before them — proofs first falls out of the deal's own order, no special case.

**The mapping.** The deal record carries which source path each dealt rule name maps to (rules are packaged one file per rule; the name is the stem). The gate checks: for every rule file already in the read log, an audit entry exists with a verbatim quote.

**Verbatim check.** `quote` must be an exact substring of the rule file's current text at the gate's resolved path. Fabrication, paraphrase and whole-file copies all fail. A quote guessed from residual memory still had to be selected and confessed, which is the ritual; that limit is accepted, not defended against with more machinery.

**Final gate unchanged.** `AuditsDealtRules` still opens everything else only when every dealt rule has an entry (quote included) and every shown owner prompt is answered with an action. Owner words stay the last gate.

**Both backends inherit it.** `DealtRuleGate(ReadsDealtSources, AuditsDealtRules)` is composed by the Claude hook (`read_gate.py`'s `GatedCall`) and Pi (`pi_rule_gate.py`'s `PiRuleGate`) through the MRO. The change is in the shared parents; subagents' own deals get it the same way. `refuse()` is backend-supplied and untouched.

**Refresh deals.** `Redeals.reread` deals rules only, at 66% and 33% context left. The same interleaving runs on the rules; the papers are still in context. Reinvestment of the ritual at every compaction and refresh is the point: a fresh instance owes a fresh confession, never a carried-over one.

**Reinjection stays a reminder.** The deck slices (`reread.py`), the SessionStart order injection and the compaction reminder text are untouched except where the reminder template is rendered from the extended schema. They remind; the gate enforces.

## Replaced

- The end-only read check in `ReadsDealtSources`: unread-list refusal becomes next-file-with-engagement.
- The paraphrase-shaped audit: `RuleAudit` gains the engagement fields; a bare `{rule, broke}` entry no longer answers the deal for a just-read rule.
- The audit template's fill instruction: it teaches the three fields with the worked example instead of "fill only broke and doing".

## Banned shapes

- A summary or paraphrase field. The failure is paraphrase; the quote is checked verbatim for exactly this.
- A promise field ("I will not hedge"). Compliant theater; failure 31 promised too.
- Engagement checked only at the end. That is the current gate, and it lost for seven hours.
- A second audit file, per-rule files or a side ledger. One audit, extended, existing paths.
- Forcing engagement on the papers. Proofs are read, not confessed; the greentexts are where the reflex lives.
- Whole-file quotes. Eight lines max — the answer must select.
- Verification theater beyond the substring check: no embedding similarity, no second gate, no score.

## Tests

In the existing gate tests (`tests/test_commander_skill_hook.py`, `tests/conftest.py` read-span helper):

1. Reading file k+1 refused while the last-read rule's entry lacks a quote; allowed once written.
2. Fabricated or paraphrased quote refused (not a substring of the file).
3. Overlong quote refused (whole-file copy).
4. Paper reads (no rule name) unlock with no engagement entry.
5. Re-reads of engaged files allowed at any point.
6. Full audit with all engagements + owner words opens the gate.
7. Refresh (rules-only) deal interleaves the same way.
8. Reminder renders the engagement template with the example.

## Architecture: the state family

`State` is the one root, asked one question: `state()`, what the gate answers now. Consumers hold the family; subclass names appear only where a case is declared.

- `ToolGateState(State)`: `enforce`, `permits_read`, and what a start or a context report does to the deal. `Open`, its session-never-dealt case `Undealt`, and `Blocked(cause)`.
- `RuleDeal(State)`: a deal is a state. `state()` is `current().answer(self)`.
- `Owed(State)`: one thing a deal still requires, with its `before` and `reason` words and `permits_read`. Cases `Unengaged`, `Unread`, `Unaudited`; `state()` is `Blocked(self)`.
- Requirement parents are declaration only: `class ReadsDealtSources(ToolGate, owes=(Unread,))`. `__init_subclass__` collects them in MRO order; `DealtRuleGate` lists `AnswersDealtRules` first, so an unanswered rule shuts the next file before "unread" is ever said.
- `DealRecord` (`.deal.json`) is persisted data, decoded once. It runs the one walk: the first owed case wins, none means `Open`. `UndealtRecord` is the record of no deal on disk.
- `judge_read`: the audit always opens; a file not dealt gets the full answer; a dealt file asks `state().permits_read(path)`.
- Edit of the audit is judged like Write on both backends, so answering a rule does not rewrite the file.
- A rule's entry is the source's stem. A deal or audit written before this gate decodes with blank answers and is re-gated; writing the answers clears it.
