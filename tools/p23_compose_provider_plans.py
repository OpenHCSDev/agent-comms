"""Compose the two retained native provider plans, correcting explicit ABC precedence."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
complementary = json.loads((ROOT / ".verification/p23-message-contract-trace/plan.json").read_text())
runtime = json.loads(Path(
    "/home/ts/wt/comms-commander-2--ac8b7b78e6634b8dad1497ab50a697c3--agent-comms/"
    ".verification/p23-native-provider-plan/runtime-native-plan.json"
).read_text())
operation, = (
    operation for recipe in runtime["recipes"] for operation in recipe["operations"]
    if operation.get("operation") == "add_class_base"
    and operation.get("target_qualname") == "NativeSession"
)
assert operation["base_name"] == "TerminalSessionRead"
operation.update(
    operation="replace_class_base", base_name="ABC", replacement_base_name="TerminalSessionRead",
    rationale="TerminalSessionRead already inherits ABC. Replace the redundant explicit ABC rather than append its subclass after it, which makes runtime C3 inconsistent. Original provider bodies and fields remain unchanged.",
)
print(json.dumps({"stages": [*complementary["stages"], runtime]}, indent=2))
