"""Bind original routing callbacks through consumed presence, namespace and result contracts."""

import argparse
import json
from pathlib import Path

from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()

    def patch(file: str, pairs: list[tuple[str, str]], target: str | None = None) -> JsonValue:
        result: dict[str, JsonValue] = {
            "operation": "patch_target", "file_path": str(args.root / file),
            "replacements": [{"old_source": old, "new_source": new} for old, new in pairs],
        }
        if target is not None:
            result["target_qualname"] = target
        return result

    def stage(name: str, reason: str, operations: list[JsonValue]) -> JsonValue:
        return {"recipes": [{"recipe_id": name, "reason": reason, "operations": operations,
                              "architecture_guards": [], "authority_claims": []}],
                "architecture_guards": []}

    tests = '''def test_origin_message_result_contract_preserves_original_providers() -> None:
    result = get_type_hints(TurnOrigin.messages)["return"]
    assert get_args(result)[0] is TurnOrigin.__type_params__[0]
    origin = get_type_hints(ScheduledTurn)["origin"]
    assert get_origin(origin) is TurnOrigin
    assert get_args(origin) == (Message,)
    assert issubclass(Message, TurnOrigin)
    assert issubclass(NoTurnOrigin, TurnOrigin)
    assert get_type_hints(NoTurnOrigin.messages)["return"] == tuple[Message, ...]


def test_routing_capture_reads_original_tagged_namespace() -> None:
    source = get_type_hints(DeliveryScope.capture)["snapshot"]
    assert get_origin(source) is RegistryNames
    owner, = get_args(source)
    assert issubclass(ThreadProvenance, owner)
    assert issubclass(Thread, owner)
    assert get_type_hints(owner)["tags"] == get_type_hints(ThreadProvenance)["tags"]
    assert get_type_hints(owner)["name"] is get_type_hints(ThreadProvenance)["name"]


def test_routing_presence_contract_keeps_original_sent_value_identity() -> None:
    assert issubclass(SentInputText, PresentMember)
    assert issubclass(NoSentInputText, NullMember)
    for routing in (Routing, TurnRouting):
        assert get_type_hints(routing.validate_input)["sent_text"] == PresentMember | NullMember


'''
    stages = [stage("p23-original-tagged-provider-and-origin-result", (
        "DeliveryScope.capture reads RegistryNames.require/aliases and the original owner's "
        "name/tags, never live activity. Actual ThreadProvenance already stores these values; "
        "give it the consumed tagged-identity capability without copying a field or changing "
        "RegistrationPolicy, identity or live Thread behavior. The new parent has no constructor "
        "or stored state and retains ThreadIdentitySource ancestry. TurnOrigin's concrete Message "
        "return annotation creates a reverse dependency into its real provider. Carry that "
        "result as a native type parameter instead, and bind the actual Message provider to "
        "itself with the ordinary self-recursive type expression. That expression is typing, "
        "not a stored/wire identity string, kind test, registry or hint cache. Every original "
        "method body/default/order and Message's existing Self return remains unchanged. "
        "The complete literal TurnOrigin map contained only the declaration, Message, "
        "NoTurnOrigin/ScheduledTurn and runtime tests; migrate all bindings together."
    ), [
        {"operation": "create_file", "file_path": str(args.root / "src/agent_comms/tagged_thread_source.py"),
         "source": '"""Original identity and tags consumed by namespace delivery selection."""\n\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource\n\n\nclass TaggedThreadSource(ThreadIdentitySource):\n    __slots__ = ()\n\n    tags: frozenset[str]\n'},
        patch("src/agent_comms/registry/threads.py", [
            ("from agent_comms.registry.thread_identity_source import ThreadIdentitySource", "from agent_comms.registry.tagged_thread_source import TaggedThreadSource"),
            ("class ThreadProvenance(ThreadIdentitySource):", "class ThreadProvenance(TaggedThreadSource):"),
        ]),
        patch("src/agent_comms/execution/turn_origin.py", [
            ("from typing import TYPE_CHECKING\n", ""),
            ("if TYPE_CHECKING:\n    from agent_comms.messages import Message\n\n", ""),
            ("class TurnOrigin(ABC):", "class TurnOrigin[MessageType](ABC):"),
            ("def messages(self) -> tuple[Message, ...]", "def messages(self) -> tuple[MessageType, ...]"),
        ]),
        patch("src/agent_comms/messages.py", [
            ("class Message(MessageInput, TurnOrigin):", 'class Message(MessageInput, TurnOrigin["Message"]):'),
        ]),
    ])]
    stages.append(stage("p23-original-routing-runtime-bindings", (
        "Routing's recorded/live namespace needs only original names, aliases and tags. "
        "Use RegistryNames specialized to the real tagged provider, preserving the complete "
        "original capture body and actual aliases mapping. Its sent-text validation asks only "
        "require_present: use the actual PresentMember/NullMember capability classes already "
        "in this module. TranscriptRoutes.record retains exact SentInputText/NoSentInputText "
        "types and digest creation; no nominal text type, null/empty-string distinction, "
        "payload identity or source decoding changes. Keep every validation body and failure "
        "message/order, incoming guidance, separate-turn batching, canonical aliases, sender "
        "lookup and publication fence unchanged. The lower origin and tagged provider remove "
        "the demonstrated reverse dependencies; bind original Delivered/CatalogDocument/Message "
        "values directly. New generic origin field and both providers retain actual Message "
        "results; no structural Protocol or forwarding store is introduced. Source parse "
        "proof is not behavior proof: qualify actual family hints, codecs, presence refusals, "
        "native routing and current/recorded namespace consumers."
    ), [
        patch("src/agent_comms/routing.py", [
            ("from typing import TYPE_CHECKING, Annotated", "from typing import Annotated"),
            ("if TYPE_CHECKING:\n    from agent_comms.bus_publication import Delivered\n    from agent_comms.catalog_document import CatalogDocument\n    from agent_comms.messages import Message\n    from agent_comms.registry_provenance import RegistryProvenance\n    from agent_comms.transcript_routes import NoSentInputText, SentInputText",
             "from agent_comms.bus_publication import Delivered\nfrom agent_comms.catalog_document import CatalogDocument\nfrom agent_comms.messages import Message\nfrom agent_comms.registry.registry_names import RegistryNames\nfrom agent_comms.registry.tagged_thread_source import TaggedThreadSource"),
            ("class NoTurnOrigin(NullMember, TurnOrigin):", "class NoTurnOrigin(NullMember, TurnOrigin[Message]):"),
            ("origin: TurnOrigin = NO_TURN_ORIGIN", "origin: TurnOrigin[Message] = NO_TURN_ORIGIN"),
            ("name: str, snapshot: RegistryProvenance, catalog: CatalogDocument", "name: str, snapshot: RegistryNames[TaggedThreadSource], catalog: CatalogDocument"),
            ("def validate_input(self, sent_text: SentInputText | NoSentInputText) -> None:\n        pass",
             "def validate_input(self, sent_text: PresentMember | NullMember) -> None:\n        pass"),
            ("def validate_input(self, sent_text: SentInputText | NoSentInputText) -> None:\n        sent_text.require_present()",
             "def validate_input(self, sent_text: PresentMember | NullMember) -> None:\n        sent_text.require_present()"),
        ]),
    ]))
    stages.append(stage("p23-original-routing-public-callback-regression", (
        "Check original providers and actual runtime result/namespace/presence contracts. "
        "Derive the new tagged source from the public capture callback rather than import "
        "a proposed new module into this test, so a source-stashed baseline can collect and "
        "fail on its original unresolved callbacks instead of a missing test import. Actual "
        "native routing, current/recorded delivery and Message serialization tests remain "
        "separate behavior proof; these positive contract checks replace no such assertion."
    ), [patch("tests/test_goal_runtime_annotations.py", [
        ("from agent_comms.nominal.field_codec import ThreadLookup", "from agent_comms.nominal.field_codec import NullMember, PresentMember, ThreadLookup"),
        ("from agent_comms.routing import NoTurnOrigin", "from agent_comms.routing import DeliveryScope, NoTurnOrigin, Routing, ScheduledTurn, TurnRouting\nfrom agent_comms.registry.registry_names import RegistryNames\nfrom agent_comms.transcript_routes import NoSentInputText, SentInputText"),
        ("def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:",
         tests + "def test_declared_policy_keys_keep_their_original_message_and_identity_sources() -> None:"),
    ])]))
    args.plan.write_text(json.dumps({"stages": stages}, indent=2) + "\n")
    print(json.dumps({"stages": len(stages)}))


if __name__ == "__main__":
    main()
