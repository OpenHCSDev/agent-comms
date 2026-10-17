"""Detached immutable binding snapshots for fixture assertions."""
from agent_comms.native_prompt_binding import expected_prompt_bindings


def binding_rows(store, input_ids, **options):
    with expected_prompt_bindings(store, input_ids, **options) as bindings:
        return bindings
