# C1: pi's vocabulary, decided once

**Repository:** agent-comms at `1f5b1f3a`. **Index:** [README.md](README.md). **Patterns:** IMPL-1, IMPL-3, MEMB-1, IDEN-3, BOUND-1. Continues S10's decoding at the pi boundary; claims nothing S10 owns.

## What is wrong

pi's values (external spellings we don't control) are compared as strings wherever they're used, with no family deciding them once:

- **Stop reasons, in two partial rosters:** `pi_events.py::apply` compares `session.stop_reason` against `aborted`, `error`, `stop`; `tracked_turn.py::assistant_end` compares `message.stop_reason` against `length`, `stop`, `toolUse`. One concept, two incomplete lists (MEMB-1).
- **Compaction reasons:** `pi_events.py::apply` compares `session.reason` against `manual`, `overflow`, `threshold`, twice.
- **Thinking levels:** `threads.py::__post_init__` validates against seven literals (`off` through `max`).
- **Payload shapes:** `pi_payloads.py::normalize` switches on `dict`, `list` and option types; `selected_pi_summary_rpc.py::_summary_response` switches over four `Summary*Data` classes that already exist, so the classes should own their response (IMPL-3).
- **Absence probing:** `pi_events.py` has the most foreign probes in the package (39) and 42 `None` checks, reading optional fields of undecoded events.
- **An imported transcript format:** `importing.py` dispatches on the format's item kinds (`compacted`, `response_item`, `turn_context`, `function_call`, …) and file suffixes in four functions.

## Target

- **Families with pi's spellings,** declared once through `declared_name`: `PiStopReason` (every value pi sends, used by both modules), `CompactionReason`, `ThinkingLevel`. Each member owns what the code now decides per comparison.
- **pi events decoded once into typed records** that own `apply`; the probes go with the optional fields.
- **Each `Summary*Data` class owns its response;** `_summary_response`'s switch is deleted. `normalize` becomes decoding.
- **Imported items as a family keyed by the external format's kinds,** decoded at import.

## Guards

No comparison against a pi stop reason, compaction reason or thinking level outside its family; the ratchet's dispatch measures in these files.

## Done when

The five dispatch sites and the two type switches are gone; `pi_events.py`'s foreign probes fall to the handful at its true boundary.

## Dispatch

> **`ac-c1`:** Complete C1 per `docs/refactor/cleanup/C1-pi-vocabulary.md`. Start with `PiStopReason`, since two modules hold partial copies of it.
