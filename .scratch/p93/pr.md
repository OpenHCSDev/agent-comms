## P93 — unfinished implementation checkpoint

**Maintenance held→READY is not implemented or accepted yet.** The existing exception still tears down forwarding; this draft is not a delivery claim.

The current checkpoint names the existing native prompt-state object `prompt_state` throughout its frame, preparation, response, watchdog and UI consumers. Kernel epoch fields and stored record keys are untouched; no compatibility alias exists. The input-forwarding interaction policy also has one declaration and all callers migrated. No source growth or new helper module.

The repair remains one owner-and-consumers change:
- MaintenancePhase/Barrier retain validated witness ownership; known held is a normal typed outcome, corruption still raises.
- InputForwarding and its inbox retain original unwritten requests until READY. No copied pending-input record, second pending list, gate exception catch or write retry loop.
- RPC pending/ACK state derives from PendingRequest's future and original command. Registration follows actual write, including the fenced original-prompt path.
- The existing prompt-state and event-reading owners determine native idle/completion, including queue changes while held; watchdog and tracked/ordinary frames must consume that same state rather than inventing another held flag or completion path.

Reused NRA syntax index before edits across Core, tests/tools, paired Toad and the pinned audit dependency: 1,064 modules, no parse errors. Expanded the existing census for RPC/pending/stats/watchdog/preparation consumers; static aliases are source evidence, not proof of arbitrary dynamic accesses. Local lifecycle imports and undefined-name checks passed. Real installed acceptance comes after the coherent implementation, in a fresh root; the six original UNKNOWN rows will never be replayed.

P71/P91 are separately merged (#1061/#1095); their acceptance is not P93 acceptance. Codex alone builds/installs. The finish check is a real saved native child: hold maintenance, enqueue one input, verify child alive and input unwritten/queued, publish READY, verify exactly one write/start/reply with no residual work. Product src must remain net <=0 against main.
