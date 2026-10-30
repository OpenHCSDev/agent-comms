---
name: prime
description: Deal this session the fleet's papers and rules again, through the rule gate, when Tristan says /prime or that the agents are misaligned or ignoring him. Variants full (default), no-papers, no-indictment, light; `--to THREAD...` primes native fleet threads instead.
---

# Prime

Tristan calls /prime when a session has drifted back to its own frame. It is a fresh deal, not a reminder: run it, then do what it prints.

1. Run in Bash, choosing the variant he named (default `full`):
   `agent-comms-prime --variant full|no-papers|no-indictment|light`
   - `full`: the papers, every rule, and for a commander the indictment and its closing rule. The same deal as a compaction.
   - `no-papers`: the rules without the papers, for when the papers are still in context.
   - `no-indictment`: the papers and rules without the indictment.
   - `light`: the rules alone. Use it mid-context so the window doesn't fill.
2. The gate now refuses every tool except reading the dealt files and writing the audit. Read each printed file in full, in the printed order, one per call. After each rule file, answer it in the audit (whole greentext lines verbatim, what they did, why it holds, what you broke) before the next file opens.
3. Answer every quoted prompt of Tristan's in the audit's `owner_words`, then do what he asked.

To prime the fleet: `agent-comms-prime --variant light --to UNIT [UNIT...]` deals each named native (Pi) thread through Core's gate. A Claude session is refused there; tell that session to run /prime itself.
