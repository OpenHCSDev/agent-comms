"""Registry-bound goal mentions are contact awareness, never work admission.

Only goal set/text-edit binds names. Reads do not resolve a fresh name to an
old token: a deleted peer's replacement cannot inherit its predecessor's goal.
"""

from __future__ import annotations

import hashlib
import re

from .declarations import GoalMentionBinding, GoalMentionSource, RegistrySnapshot, Thread

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
            bindings = [GoalMentionBinding("<goal-mentions>", "limit_exceeded")]
            break
        seen.add(token)
        peer = registry.threads.get(token)
        if any(char in token for char in "./\\@"):
            bindings.append(GoalMentionBinding(token, "malformed"))
        elif token == owner.name:
            bindings.append(GoalMentionBinding(token, "self"))
        elif peer is None:
            bindings.append(
                GoalMentionBinding(token, "alias" if token in registry.aliases else "unknown")
            )
        elif not peer.role.executable:
            bindings.append(GoalMentionBinding(token, "non_executable"))
        else:
            bindings.append(GoalMentionBinding(token, "resolved", peer.name, peer.created_at))
    return GoalMentionSource(
        goal_id,
        revision,
        hashlib.sha256(text.encode("utf-8")).hexdigest(),
        owner.name,
        owner.created_at,
        tuple(bindings),
    )
