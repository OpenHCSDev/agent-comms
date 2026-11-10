"""Finish the original Git-declared module identity mapping before package placement."""

import json

from p23_rebase_imports import ModuleMoves
from p23_revision_owner_plan import SRC, patch

moves = ModuleMoves.at("33af0b93de5e1e360502bd4801373aff38933973")
plan = moves.import_recipe()
operations = plan["recipes"][0]["operations"]
# These ordinary aliased module imports are outside ImportFrom normalization.
# Both preserve the original local binding and read the same moved declarations.
for filename, original, binding in (
    ("input_disposition.py", "agent_comms.bus", "bus"),
    ("execution/input_origin.py", "agent_comms.retained_task_facts", "task_facts"),
):
    destination = moves.modules[original]
    old = f"import {original} as {binding}"
    new = f"import {destination} as {binding}"
    same_file = [operation for operation in operations
                 if operation["file_path"] == str(SRC / filename)]
    if same_file:
        operation, = same_file
        replacement, = operation["replacements"]
        if old not in replacement["new_source"]:
            raise ValueError(f"Original module alias changed: {filename}:{old}")
        replacement["new_source"] = replacement["new_source"].replace(old, new)
    else:
        operations.append(patch(SRC / filename, [(old, new)]))
print(json.dumps(plan, indent=2))
