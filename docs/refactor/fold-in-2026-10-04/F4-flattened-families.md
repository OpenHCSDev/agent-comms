# F4: Families leaving their owners as strings

**Index:** [README.md](README.md). **After F0.** **Pattern:** BOUND-8.

## What is wrong

A family member's own name used as the value that crosses a boundary (`x.declared_name` passed into a call, returned, or chosen with `or`) means consumers must decide its meaning again, and the string can carry values the family lacks. The refactor-audit census measure `family_flattened` finds 0.88 such sites per 1,000 lines in agent-comms and 0.34 in Toad, including `pi_events.py`'s `CompactionStart(reason=self.reason.declared_name)`.

## Target

1. **Triage every site.** Codec and schema modules that generate wire or SQL forms from a family (`typed_table.py`, `coordination_schema.py`) are the mechanism and stay. Every other site carries the member itself, encoded by the codec.
2. **Then ratchet it,** with the codec and schema modules exempt by name, so no touched file can add a new flattening.

## Done when

Every remaining site is a codec or schema module, and the measure is in the required ratchet.
