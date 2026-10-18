"""Registry-bound goal mentions are contact awareness, never work admission.

Only goal set/text-edit binds names. Reads do not resolve a fresh name to an
old token: a deleted peer's replacement cannot inherit its predecessor's goal.
"""

from __future__ import annotations

import hashlib
import re

from .goals import (
    GoalMentionBinding,
    GoalMentionSource,
    LimitExceededMentionBinding,
    MalformedMentionBinding,
    SelfMentionBinding,
    AliasMentionBinding,
    UnknownMentionBinding,
    NonExecutableMentionBinding,
    ResolvedMentionBinding,
)
from .registry_document import RegistrySnapshot
from .threads import Thread

# Unlike message mentions, a dotted/path/email suffix must not yield a partial
# thread name. Aliases are diagnostics at authoring, not new peer identities.
_GOAL_TOKEN = re.compile(
    r"(?<![\w@/\\])@([A-Za-z0-9_-]+(?:[./\\@][A-Za-z0-9_-]+)*)(?![\w@/\\]|\.[A-Za-z0-9_.-])"
)
_MAX_BINDINGS = 128
_MAX_TOKEN_CHARS = 512


def bind_goal_mentions(
    text: str, goal_id: str, revision: int, owner: Thread, registry: RegistrySnapshot
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
        elif not peer.role.executable:
            bindings.append(NonExecutableMentionBinding(token=token))
        else:
            bindings.append(
                ResolvedMentionBinding(
                    token=token, peer_name=peer.name, peer_created_at=peer.created_at
                )
            )
    return GoalMentionSource(
        goal_id,
        revision,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        owner.name,
        owner.created_at,
        tuple(bindings),
    )
