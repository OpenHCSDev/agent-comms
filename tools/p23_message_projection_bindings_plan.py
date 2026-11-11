"""Keep message projection algorithms lazy while binding their actual table modules globally."""

import json
import re

from p23_revision_owner_plan import SRC, patch, stage

path = SRC / "messaging/messages.py"
before = path.read_text()
after = before
imports = (
    "        from agent_comms.bus_tables import AwaitingAnswerSource\n",
    "        from agent_comms.messaging.bus_route_counts import BusRouteCounts, BusRouteKeys\n",
    "        from agent_comms.bus_activity_index import SentActivity\n",
    "        from agent_comms.bus_tables import MessageIdentitySource\n",
    "        from agent_comms.bus_activity_index import BusActivityIndex, SentActivity\n",
    "        from agent_comms.bus_tables import AnsweredSource, MessageIdentitySource\n",
    "        from agent_comms.messaging.bus_display_index import BusDisplayIndex\n",
    "        from agent_comms.bus_activity_index import BusActivityIndex\n",
)
for statement in imports:
    if statement not in after:
        raise ValueError(f"Original deferred projection import changed: {statement}")
    after = after.replace(statement, "")
for name, owner in (
    ("AwaitingAnswerSource", "bus_tables"), ("AnsweredSource", "bus_tables"),
    ("MessageIdentitySource", "bus_tables"), ("BusRouteCounts", "bus_route_counts"),
    ("BusRouteKeys", "bus_route_counts"), ("SentActivity", "bus_activity_index"),
    ("BusActivityIndex", "bus_activity_index"), ("BusDisplayIndex", "bus_display_index"),
):
    after = re.sub(rf"(?<!\.)\b{name}\b", f"{owner}.{name}", after)
after = after.rstrip() + '''
from agent_comms import bus_activity_index, bus_tables
from agent_comms.messaging import bus_display_index, bus_route_counts
'''
reason = (
    "The retained complete-use search shows these eight names only in their original function-local table imports, ClassFact writes getters and runtime projection/reference methods; no local assignment or shadowed declaration exists for them. "
    "Use their original modules after every Message/BusRecord/MessageHistory declaration has completed, so the actual BusProjectionTables eager base exists before table modules initialize. "
    "Keep every ClassFact decorator, getter, tuple, constructor argument and call order unchanged. "
    "class_fact.py:31-49 keeps derivation lazy on descriptor access, while FamilyMeta.__new__ invalidates membership-derived facts through Declarations.changed after each declaration. "
    "This changes module acquisition, not the time or algorithm of ClassFact derivation; the original SQL classes are neither copied, replaced nor made into empty schema declarations. "
    "The new ordinary module bindings and their cold entry orders still require the combined runtime/schema/consumer proof; source spelling alone is not that proof."
)
print(json.dumps({"stages": [stage("p23-bind-original-message-projection-modules-after-records", reason,
                                  [patch(path, [(before, after)])])]}, indent=2))
