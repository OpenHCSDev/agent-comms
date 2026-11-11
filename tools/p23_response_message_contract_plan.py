"""Name the original message facts the response-policy family actually reads."""

import argparse
import ast
import json
from pathlib import Path

from nominal_refactor_advisor.ast_tools import parse_python_modules
from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    policy_path = args.root / "src/agent_comms/response_policy.py"
    module, = parse_python_modules(policy_path, parse_workers=1)
    role_source = '''"""Original message facts read by response-policy implementations."""

from abc import ABC

from agent_comms.mentions import ThreadMention


class ResponseMessageSource(ABC):
    __slots__ = ()

    seq: int
    mentions: tuple[ThreadMention, ...]
'''
    operations: list[JsonValue] = [{
        "operation": "create_file",
        "file_path": str(args.root / "src/agent_comms/response_message_source.py"),
        "source": role_source,
    }, {
        "operation": "patch_target", "file_path": str(args.root / "src/agent_comms/messages.py"),
        "replacements": [{
            "old_source": "from agent_comms.messaging.message_sender import PublicSender",
            "new_source": "from agent_comms.messaging.message_sender import PublicSender\nfrom agent_comms.messaging.response_message_source import ResponseMessageSource",
        }, {
            "old_source": "class MessageInput(Timestamped, Addressed, BusCapability):",
            "new_source": "class MessageInput(Timestamped, Addressed, BusCapability, ResponseMessageSource):",
        }],
    }, {
        "operation": "patch_target", "file_path": str(policy_path),
        "replacements": [{"old_source": "from typing import TYPE_CHECKING\n", "new_source": ""}, {
            "old_source": "if TYPE_CHECKING:\n    from agent_comms.messages import Message, MessageInput\n",
            "new_source": "",
        }, {
            "old_source": "from agent_comms.projection_source import ProducedRow, ProjectionSource",
            "new_source": "from agent_comms.projection_source import ProducedRow, ProjectionSource\nfrom agent_comms.messaging.response_message_source import ResponseMessageSource",
        }],
    }]
    selected = []
    for owner in module.syntax_index.instances_of(ast.ClassDef):
        assert owner in module.module.body, "A nested or conditional policy class needs its own ownership decision"
        for statement in owner.body:
            match statement:
                case ast.FunctionDef(args=parameters):
                    for parameter in (*parameters.posonlyargs, *parameters.args, *parameters.kwonlyargs):
                        match parameter:
                            case ast.arg(arg="message", annotation=ast.Name(id="Message" | "MessageInput" as name)):
                                target = f"{owner.name}.{statement.name}"
                                operations.append({
                                    "operation": "patch_target", "file_path": str(policy_path),
                                    "target_qualname": target,
                                    "replacements": [{"old_source": f"message: {name}",
                                                      "new_source": "message: ResponseMessageSource"}],
                                })
                                selected.append(target)
    assert selected
    reason = (
        "P23 requires actual runtime callback types, not guarded annotations or structural "
        "Protocol substitutes. The original response-policy family reads only message.seq "
        "for its disposition key and message.mentions for MentionedPolicy; the other policy "
        "methods use audience/name/alias arguments or their own declared behavior. The original "
        "MessageInput already declares both fields and is the real provider at every selected "
        "call. Give it ResponseMessageSource through nominal inheritance; policies import that "
        "lower consumed role instead of the complete message assembly. Keep every method body, "
        "argument position, default, key constructor, conversion, recipient order, alias read "
        "and guidance byte unchanged. The complete direct ClassDef/method census comes from "
        "NRA's parsed ModuleSyntaxIndex, with nested/conditional owners refused, not a second "
        "parser or hand-maintained policy list. This is a callback contract repair, not a new "
        "message state or copied data carrier: the role has no constructor or stored fields, "
        "and the original MessageInput dataclass remains the sole value declaration. "
        "No local replacement import is added. The existing lazy projection-table imports "
        "are not claimed closed by this type repair. This authored annotation/MI decision "
        "requires runtime hint, actual MRO, policy new-case, direct/owned key and unchanged "
        "serialization/consumer qualification; empty architecture guards prove none of those."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-original-response-message-contract",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"selected_methods": selected, "operations": len(operations)}))


if __name__ == "__main__":
    main()
