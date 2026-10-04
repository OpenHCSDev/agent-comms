"""Selected response contributor borrows the original route and grammar."""
from dataclasses import dataclass
import json
from ..field_codec import FieldCodec
from ..messages import Message, MessageType
from ..turn_context import InstructionFile, ReplyRouteSegment

@dataclass(frozen=True, kw_only=True)
class SelectedResponseSegment(ReplyRouteSegment):
    """Selected response proposals use the existing route owner and grammar."""

    sender: str
    example: InstructionFile

    def values(self):
        examples = tuple(
            Message(self.sender, target, self.example.content, MessageType.INFO, timestamp=0)
            for target in self.targets
        )
        return dict(examples=json.dumps(FieldCodec.encode(examples)))
