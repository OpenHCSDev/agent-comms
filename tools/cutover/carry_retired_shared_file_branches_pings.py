"""Retire board pings of the deleted SharedFileBranches rule before decoding packets.

#3623 replaced it with ConflictingBranches; the stored pings still name the old rule, so
every heartbeat died decoding them. Dropped, the heartbeat raises real conflicts once more
under the new rule. Retire this step after the retained buses have crossed it.
"""

from cutover.carry_rule_ping_types import RulePingTypes


class RetiredSharedFileBranchesPings(RulePingTypes):
    predicate = "json_extract(value, :rule) = 'shared_file_branches'"
