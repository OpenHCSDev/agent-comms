---
name: prime
description: Deal this session the fleet's papers and rules again, through the rule gate, when Tristan says /prime or that the agents are misaligned or ignoring him. Variants full (default), no-papers, no-patterns, light; `--to THREAD...` primes native fleet threads instead.
---

# Prime

Tristan calls /prime when a session has drifted back to its own frame. It is a fresh deal, not a reminder: run it, then do what it prints.

1. Run in Bash, choosing the variant he named (default `full`):
   `agent-comms-prime --variant full|no-papers|no-patterns|light`
   - `full`: the papers, every rule, and the pattern catalog. The same deal as a compaction.
   Every variant keeps every rule, including the closing rules dealt last.
   - `no-papers`: the rules without the papers, for when the papers are still in context.
   - `no-patterns`: the papers and rules without the pattern catalog.
   - `light`: the rules alone. Use it mid-context so the window doesn't fill.
2. Until the deal is read and answered, the gate refuses every tool except reading the dealt files and the audit, writing the audit, loading a tool's schema, and a Monitor running the bus watch. Read each printed file in full, in the printed order. The files open in batches whose size is declared once, `Unread.batch` in `agent_comms/team/rule_deal.py`; every refusal names the size and the next batch, so read the whole batch in one parallel call. Then answer the audit's costly probes (`agent_comms/projects/rule_probes.py`): each refusal names the probe still owed and its question.
3. Answer every quoted prompt of Tristan's in the audit's `owner_words`, then do what he asked.

To prime the fleet: `agent-comms-prime --variant light --to UNIT [UNIT...]` deals each named native (Pi) thread through Core's gate. A Claude session is refused there; tell that session to run /prime itself.
