"""Keep disposition policies below incarnation/role assembly by consuming original creation values."""

import argparse
import json
from pathlib import Path

from pydantic import JsonValue


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    operations: list[JsonValue] = []

    def patch(file: str, pairs: list[tuple[str, str]]) -> None:
        operations.append({"operation": "patch_target", "file_path": str(args.root / file),
                           "replacements": [{"old_source": old, "new_source": new} for old, new in pairs]})

    patch("src/agent_comms/registry/thread_creation.py", [
        ("class RecordedCreationTime(CreationTime):",
         'class ThreadCreationSource(ABC):\n    """The original creation value used to distinguish an owner-scoped input key."""\n\n    __slots__ = ()\n    created_at: CreationTime\n\n\nclass RecordedCreationTime(CreationTime):'),
    ])
    patch("src/agent_comms/thread_identity_source.py", [
        ("from abc import ABC, abstractmethod", "from abc import abstractmethod"),
        ("from agent_comms.registry.thread_creation import CreationTime",
         "from agent_comms.registry.thread_creation import ThreadCreationSource"),
        ("class ThreadIdentitySource(ABC):", "class ThreadIdentitySource(ThreadCreationSource):"),
        ("    created_at: CreationTime\n", ""),
    ])
    patch("src/agent_comms/messages.py", [
        ("from agent_comms.registry.thread_identity_source import ThreadIdentitySource",
         "from agent_comms.registry.thread_identity_source import ThreadIdentitySource\nfrom agent_comms.registry.thread_creation import ThreadCreationSource"),
        ("def bus_key(self, owner: ThreadIdentitySource)", "def bus_key(self, owner: ThreadCreationSource)"),
        ("def bus_keys(self, owner: ThreadIdentitySource)", "def bus_keys(self, owner: ThreadCreationSource)"),
    ])
    for file in ("src/agent_comms/execution/turn_origin.py", "src/agent_comms/routing.py"):
        patch(file, [
            ("from agent_comms.registry.thread_identity_source import ThreadIdentitySource",
             "from agent_comms.registry.thread_creation import ThreadCreationSource"),
            ("def bus_keys(self, owner: ThreadIdentitySource)", "def bus_keys(self, owner: ThreadCreationSource)"),
        ])
    patch("src/agent_comms/response_policy.py", [
        ("from agent_comms.registry.thread_identity_source import ThreadIdentitySource",
         "from agent_comms.registry.thread_creation import ThreadCreationSource"),
        ("    def disposition_key(self, message: ResponseMessageSource, owner: ThreadIdentitySource) -> InputKey:\n        return OwnedBusInputKey(message.seq, float(owner.created_at))",
         "    def disposition_key(self, message: ResponseMessageSource, owner: ThreadCreationSource) -> InputKey:\n        return OwnedBusInputKey(message.seq, float(owner.created_at))"),
        ("    def disposition_key(self, message: ResponseMessageSource, owner: ThreadIdentitySource) -> InputKey:\n        return BusInputKey(message.seq)",
         "    def disposition_key(self, message: ResponseMessageSource, owner: ThreadCreationSource) -> InputKey:\n        return BusInputKey(message.seq)"),
    ])
    patch("tests/test_goal_runtime_annotations.py", [
        ("from agent_comms.registry.thread_identity_source import ThreadIdentitySource",
         "from agent_comms.registry.thread_identity_source import ThreadIdentitySource\nfrom agent_comms.registry.thread_creation import ThreadCreationSource"),
        ("    assert Message.bus_key is MessageInput.bus_key\n",
         "    assert Message.bus_key is MessageInput.bus_key\n    assert issubclass(ThreadIdentitySource, ThreadCreationSource)\n    assert issubclass(Thread, ThreadCreationSource)\n"),
        ("    for callback in (MessageInput.bus_key, Message.bus_keys, NoTurnOrigin.bus_keys,\n                     TurnOrigin.bus_keys, ClaimNoticeKind.claimed):\n        assert get_type_hints(callback)[\"owner\"] is ThreadIdentitySource",
         "    for callback in (MessageInput.bus_key, Message.bus_keys, NoTurnOrigin.bus_keys,\n                     TurnOrigin.bus_keys):\n        assert get_type_hints(callback)[\"owner\"] is ThreadCreationSource\n    assert get_type_hints(ClaimNoticeKind.claimed)[\"owner\"] is ThreadIdentitySource"),
        ("        assert hints[\"owner\"] is ThreadIdentitySource\n        assert hints[\"return\"] is InputKey",
         "        assert hints[\"owner\"] is ThreadCreationSource\n        assert hints[\"return\"] is InputKey"),
    ])
    reason = (
        "The first key-consumer run failed before collection: child_process -> thread_identity "
        "-> response_policy -> ThreadIdentitySource -> partly initialized thread_identity. "
        "Retain that exit-4 receipt, not passing proof. The policy does not need incarnation "
        "or role assembly: its only owner read is float(owner.created_at). Put that existing "
        "CreationTime-valued read contract beside the original creation family, and have the "
        "existing ThreadIdentitySource inherit it. ThreadProvenance and Thread retain the "
        "same actual field, constructor and values; there is no timestamp copy, derived-birth "
        "translation, new state or guarded/local replacement import. Key callbacks consume "
        "ThreadCreationSource while ClaimNoticeKind.claimed still consumes the lower name/"
        "identity role. Both policies' function bodies and key constructors remain byte-for-"
        "byte unchanged. No thread-role family is moved. Runtime provider/MRO/hint assertions "
        "and the original policy/channel/batch/notification/goal consumers must qualify the "
        "written source; an empty guard suite proves no behavior."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-original-creation-key-source",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
