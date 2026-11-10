"""Compose the stable callback suffix with the demonstrated BusReader C3 repair."""

import json
from pathlib import Path

from p23_revision_owner_plan import SRC, stage

suffix = Path("/home/ts/wt/comms-commander-2--ac8b7b78e6634b8dad1497ab50a697c3--agent-comms/.verification/p23-runtime-callback-plan/callback-suffix.plan.json")
plan = json.loads(suffix.read_text())
plan["stages"].insert(0, stage(
    "p23-remove-busreader-redundant-abc",
    "BusReader lists ABC before InputPublicationSource although that source inherits InputPublicationRead(ABC). Remove the redundant direct ancestor; all original capabilities, acquisition methods and abstract requirements remain inherited.",
    [{"operation": "remove_class_base", "file_path": str(SRC / "bus.py"),
      "target_qualname": "BusReader", "base_name": "ABC"}],
))
print(json.dumps(plan, indent=2))
