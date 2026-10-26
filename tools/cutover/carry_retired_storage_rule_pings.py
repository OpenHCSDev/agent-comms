"""Retire notifications of the deleted per-unit storage quota before decoding packets.

The original message text, envelope and every other ping remain intact. This carries
notification metadata, not user input or scientific evidence. Retire this step after
the retained buses have crossed it.
"""

from cutover.carry_commander_update_lines import CommanderUpdateLines
from cutover.carry_rule_ping_types import RulePingTypes


class RetiredStorageRulePings(RulePingTypes):
    precedes = (CommanderUpdateLines,)
    predicate = "json_extract(value, :rule) = 'storage_over_quota'"
