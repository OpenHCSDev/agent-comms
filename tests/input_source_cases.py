"""Current original-source fixtures; no alternate production representation."""

from agent_comms.channel_input_batch import SingleInputBatch
from agent_comms.turn_goal_permission import OwnerGoalPermission
from agent_comms.turn_input_source import OwnerOriginalInput, NoInputDependency


def owner_original(keys, text):
    return OwnerOriginalInput(
        keys=tuple(keys),
        accepted_id=None,
        goal_permission=OwnerGoalPermission(None),
        prompt=text,
        original_display=text,
        origins=(),
        dependency=NoInputDependency(),
        batch=SingleInputBatch(),
    )
