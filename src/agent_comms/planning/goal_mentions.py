from __future__ import annotations

import hashlib
import re

from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goals import (
    AliasMentionBinding,
    GoalMentionBinding,
    GoalMentionSource,
    LimitExceededMentionBinding,
    MalformedMentionBinding,
    NonExecutableMentionBinding,
    ResolvedMentionBinding,
    SelfMentionBinding,
    UnknownMentionBinding,
)
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread

_GOAL_TOKEN = re.compile(
    r"(?<![\w@/\\])@([A-Za-z0-9_-]+(?:[./\\@][A-Za-z0-9_-]+)*)(?![\w@/\\]|\.[A-Za-z0-9_.-])"
)


_MAX_BINDINGS = 128


_MAX_TOKEN_CHARS = 512


def bind_goal_mentions(
    text: str, goal_id: GoalId, revision: int, owner: Thread, registry: RegistrySnapshot
) -> GoalMentionSource:
    """Freeze exact registered executable incarnations in the same Goal row."""
    bindings: list[GoalMentionBinding] = []
    seen: set[str] = set()
    for match in _GOAL_TOKEN.finditer(text):
        token = match.group(1)
        if token in seen:
            continue
        if len(token) > _MAX_TOKEN_CHARS or len(bindings) >= _MAX_BINDINGS:
            # Never present an incomplete subset as the owner's complete goal
            # contact set. Only the diagnostic survives an over-limit goal.
            bindings = [LimitExceededMentionBinding(token="<goal-mentions>")]
            break
        seen.add(token)
        peer = registry.threads.get(token)
        if any(char in token for char in "./\\@"):
            bindings.append(MalformedMentionBinding(token=token))
        elif token == owner.name:
            bindings.append(SelfMentionBinding(token=token))
        elif peer is None:
            bindings.append(
                (AliasMentionBinding if token in registry.aliases else UnknownMentionBinding)(
                    token=token
                )
            )
        else:
            bindings.append(
                next(
                    (
                        ResolvedMentionBinding(
                            token=token,
                            peer_name=executor.name,
                            peer_created_at=executor.incarnation.wire_birth,
                        )
                        for executor in peer.role.executor(peer)
                    ),
                    NonExecutableMentionBinding(token=token),
                )
            )
    return GoalMentionSource(
        goal_id=goal_id,
        text_revision=revision,
        text_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        owner_name=owner.name,
        owner_created_at=owner.incarnation.wire_birth,
        bindings=tuple(bindings),
    )
