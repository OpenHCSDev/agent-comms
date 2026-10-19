"""Frontend-neutral message categories and agent-output grouping."""

from abc import abstractmethod
from ..declared_family import DeclaredFamily


class MessageCategory(DeclaredFamily, affix="Category"):
    label: str

    @classmethod
    @abstractmethod
    def observe(cls, boundary) -> bool: ...


class FromPerson:
    @classmethod
    def observe(cls, boundary) -> bool:
        boundary.reset()
        return False


class FromAgent:
    @classmethod
    def observe(cls, boundary) -> bool:
        boundary.finish()
        return False


class AgentWork:
    @classmethod
    def observe(cls, boundary) -> bool:
        return boundary.consume()


class UserCategory(FromPerson, MessageCategory):
    label = "User messages"


class AgentCategory(FromAgent, MessageCategory):
    label = "Agent messages"


class InboundCategory(FromPerson, MessageCategory):
    label = "Messages in"


class OutboundCategory(FromAgent, MessageCategory):
    label = "Messages out"


class ThinkingCategory(AgentWork, MessageCategory):
    label = "Thinking"


class ToolCategory(AgentWork, MessageCategory):
    label = "Tool calls"


class OtherCategory(MessageCategory):
    label = "Other / notices"

    @classmethod
    def observe(cls, boundary) -> bool:
        return False


def all_categories() -> frozenset[type[MessageCategory]]:
    return frozenset(MessageCategory.members_with(MessageCategory))


class AgentActivityBoundary:
    """Group thinking/tools only when no preceding agent content supplied a header."""

    def __init__(self) -> None:
        self._pending = True

    def reset(self) -> None:
        self._pending = True

    def finish(self) -> None:
        self._pending = False

    def consume(self) -> bool:
        pending = self._pending
        self.finish()
        return pending

    def observe(self, category: type[MessageCategory]) -> bool:
        return category.observe(self)
