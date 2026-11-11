"""Move original message-key dispatch off the input store and migrate its full caller set."""

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

    def patch(file: str, replacements: list[tuple[str, str]]) -> None:
        operations.append({"operation": "patch_target", "file_path": str(args.root / file),
                           "replacements": [{"old_source": before, "new_source": after}
                                            for before, after in replacements]})

    patch("src/agent_comms/thread_identity_source.py", [
        ("from agent_comms.registry.thread_identity import ThreadIncarnation",
         "from agent_comms.registry.thread_creation import CreationTime\nfrom agent_comms.registry.thread_identity import ThreadIncarnation"),
        ("    name: str\n", "    name: str\n    created_at: CreationTime\n"),
    ])
    patch("src/agent_comms/messages.py", [
        ("from agent_comms.messaging.message_sender import PublicSender",
         "from agent_comms.messaging.message_sender import PublicSender\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource"),
        ("    from agent_comms.input_disposition import InputDispositions\n", ""),
        ("    from agent_comms.registry.threads import Thread\n", ""),
        ("    def claimed(cls, owner: Thread) -> Message:",
         "    def claimed(cls, owner: ThreadIdentitySource) -> Message:"),
        ("    @property\n    def sender_label(self) -> str:",
         "    def bus_key(self, owner: ThreadIdentitySource) -> InputKey:\n        return self.role.response_policy(self).disposition_key(self, owner)\n\n    @property\n    def sender_label(self) -> str:"),
        ("    def bus_keys(self, dispositions: InputDispositions, owner: Thread) -> tuple[InputKey, ...]:\n        return (dispositions.bus_key(self, owner),)",
         "    def bus_keys(self, owner: ThreadIdentitySource) -> tuple[InputKey, ...]:\n        return (self.bus_key(owner),)"),
    ])
    patch("src/agent_comms/execution/turn_origin.py", [
        ("from agent_comms.input_key import InputKey",
         "from agent_comms.input_key import InputKey\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource"),
        ("    from agent_comms.input_disposition import InputDispositions\n", ""),
        ("    from agent_comms.registry.threads import Thread\n", ""),
        ("def bus_keys(self, dispositions: InputDispositions, owner: Thread)",
         "def bus_keys(self, owner: ThreadIdentitySource)"),
    ])
    patch("src/agent_comms/routing.py", [
        ("from agent_comms.response_policy import NO_ALIASES",
         "from agent_comms.response_policy import NO_ALIASES\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource"),
        ("    from agent_comms.input_disposition import InputDispositions\n", ""),
        ("    from agent_comms.registry.threads import Thread\n", ""),
        ("def bus_keys(self, dispositions: InputDispositions, owner: Thread)",
         "def bus_keys(self, owner: ThreadIdentitySource)"),
    ])
    patch("src/agent_comms/response_policy.py", [
        ("from agent_comms.projection_source import ProducedRow, ProjectionSource",
         "from agent_comms.projection_source import ProducedRow, ProjectionSource\nfrom agent_comms.registry.thread_identity_source import ThreadIdentitySource"),
        ("    from agent_comms.registry.threads import Thread\n", ""),
        ("    def disposition_key(self, message: MessageInput, owner: Thread) -> InputKey:\n        return OwnedBusInputKey(message.seq, float(owner.created_at))",
         "    def disposition_key(self, message: MessageInput, owner: ThreadIdentitySource) -> InputKey:\n        return OwnedBusInputKey(message.seq, float(owner.created_at))"),
        ("    def disposition_key(self, message: MessageInput, owner: Thread) -> InputKey:\n        return BusInputKey(message.seq)",
         "    def disposition_key(self, message: MessageInput, owner: ThreadIdentitySource) -> InputKey:\n        return BusInputKey(message.seq)"),
    ])
    patch("src/agent_comms/input_disposition.py", [
        ("    @staticmethod\n    def bus_key(message: MessageInput, owner: Thread) -> InputKey:\n        return message.role.response_policy(message).disposition_key(message, owner)\n\n", ""),
        ("self.bus_key(message, entry.thread)", "message.bus_key(entry.thread)"),
    ])
    for file, replacements in (
        ("src/agent_comms/channel_input_batch.py", [("InputDispositions.bus_key(origin, owner)", "origin.bus_key(owner)")]),
        ("src/agent_comms/execution/input_drain.py", [("turn.origin.bus_keys(self.dispositions, owner)", "turn.origin.bus_keys(owner)")]),
        ("src/agent_comms/execution/owned_turn.py", [("self.runner.inputs.dispositions.bus_key(origin, self.thread)", "origin.bus_key(self.thread)")]),
        ("src/agent_comms/notification_assignment.py", [("InputDispositions.bus_key(page.source.message, thread)", "page.source.message.bus_key(thread)")]),
        ("src/agent_comms/planning/goal_records.py", [("InputDispositions.bus_key(original.message, thread)", "original.message.bus_key(thread)")]),
        ("src/agent_comms/queued_input.py", [("InputDispositions.bus_key(source.message, owner)", "source.message.bus_key(owner)"),
                                           ("InputDispositions.bus_key(message, owner)", "message.bus_key(owner)")]),
        ("tests/test_channel_input_batch.py", [("ledger.bus_key(message, owner)", "message.bus_key(owner)")]),
        ("tests/test_coordination_response.py", [("inputs.bus_key(follow_up, case.witness.thread)", "follow_up.bus_key(case.witness.thread)")]),
        ("tests/test_following_context_notifications.py", [("InputDispositions.bus_key(message.message, owner)", "message.message.bus_key(owner)")]),
        ("tests/test_historical_notifications.py", [("inputs.bus_key(original, owner)", "original.bus_key(owner)")]),
    ):
        patch(file, replacements)
    patch("tests/test_response_policy.py", [
        ("from agent_comms.messages import ConversationRole, InfoKind, Message",
         "from agent_comms.messages import ConversationRole, InfoKind, Message\nfrom agent_comms.input_key import BusInputKey, OwnedBusInputKey"),
        ("def test_new_policy_drives_eligibility_guidance_and_batching(tmp_path):",
         "def test_new_policy_drives_eligibility_guidance_and_batching(tmp_path, thread_registration):"),
        ("        assert ScheduledTurn.take_batch([turn, turn]) == ([turn], [turn])",
         "        assert ScheduledTurn.take_batch([turn, turn]) == ([turn], [turn])\n        owner = NativeThread(\"reviewer\", frozenset(), Checkout(tmp_path))\n        assert message.bus_key(owner) == OwnedBusInputKey(message.seq, float(owner.created_at))"),
        ("    from agent_comms.input_disposition import InputDispositions\n", ""),
        ("    dispositions = InputDispositions(tmp_path)\n", ""),
        ("goal.origin.bus_keys(dispositions, owner)", "goal.origin.bus_keys(owner)"),
        ("    assert first.origin.bus_keys(dispositions, owner) == (dispositions.bus_key(first.origin, owner),)",
         "    assert first.origin.bus_keys(owner) == (OwnedBusInputKey(first.origin.seq, float(owner.created_at)),)\n    assert direct.origin.bus_keys(owner) == (BusInputKey(direct.origin.seq),)"),
    ])
    reason = (
        "Admitted by P23's dependency-direction repair and the existing one-owner/no-forwarding "
        "rules: a key is determined by the original message role's response policy and, for an "
        "owned key, the original owner's creation value. InputDispositions.bus_key is static, "
        "reads no store state and forwards exactly that existing dispatch. Move that exact body "
        "onto MessageInput, which already owns the role/type/sequence; all real message inputs "
        "inherit it. Remove the store copy rather than retaining a proxy or adding a new helper. "
        "No policy choice, key constructor, creation-value conversion, root binding, write, "
        "generation, scheduler authority or UNKNOWN refusal changes. DirectPolicy still owns "
        "BusInputKey and all other policies retain OwnedBusInputKey. ThreadIdentitySource "
        "includes its actual providers' original CreationTime field, not a copied timestamp or "
        "incarnation-to-float translation; ThreadProvenance/Thread already supply it. Claim "
        "construction consumes the same lower name/identity provider. Drop only the unused "
        "dispositions argument through TurnOrigin/Message/NoTurnOrigin bus_keys and migrate "
        "the complete indexed Core/tests call inventory. InputDrain.dispositions is an eager "
        "constructor-assigned field at input_drain.py:902, so removing its unused read does not "
        "skip a getter or store initializer. No bus_key override exists in the selected corpus. "
        "The actual response_policy implementations remain independent of starts_turn flags; "
        "the existing new-policy test now checks its inherited owned key, while direct and "
        "autonomous cases check their declared constructors rather than a tautological wrapper. "
        "Existing native provider inheritance and exact method return contracts remain. "
        "Receiver migration is an authored exact-target DSL patch: the installed catalog has "
        "no operation proving this static-first-argument to bound-owner conversion, so this is "
        "not an automatic equivalence claim. Qualify all original key/policy/batch/notification/"
        "queue/goal/runtime consumers and actual runtime hints. Other disclosed guarded "
        "callback cycles remain OPEN and this operation does not claim complete closure."
    )
    args.plan.write_text(json.dumps({"recipes": [{"recipe_id": "p23-original-message-key-owner",
                                                "reason": reason, "operations": operations,
                                                "architecture_guards": [], "authority_claims": []}],
                                     "architecture_guards": []}, indent=2) + "\n")
    print(json.dumps({"operations": len(operations)}))


if __name__ == "__main__":
    main()
