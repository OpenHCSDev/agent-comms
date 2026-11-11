"""Author and simulate the guarded-import cut at the assigned Core commit; never apply."""
from __future__ import annotations

import ast
import difflib
import json
import subprocess
import sys
from pathlib import Path

from nominal_refactor_advisor.codemod import CodemodPlanRoot, CodemodSourceSnapshot
from nominal_refactor_advisor.codemod_preflight import CodemodOperationPreflightError
from nominal_refactor_advisor.json_reports import json_report_object

ARTIFACTS = Path(__file__).resolve().parent
ROOT = ARTIFACTS.parents[1]
HEAD = "b47c7c0ded5005be39bfcf9e129297fabc4f127d"
CLOCK_SOURCE = '''"""Original recorded message clocks, below destination and persistence initialization."""

from abc import ABC, abstractmethod

from agent_comms.recorded_time import Timestamped


class SenderClockSource(ABC):
    __slots__ = ()

    @classmethod
    @abstractmethod
    def last_user_input(cls, previous: float, timestamp: float) -> float: ...


class MessageClockSource(Timestamped):
    __slots__ = ()

    sender_role: type[SenderClockSource]
'''
THREAD_ROLE_SOURCE = '''"""Original authorship role, independent of thread runtime assembly."""

from abc import ABC
from agent_comms.registry.thread_identity import ThreadRole


class ThreadRoleSource(ABC):
    __slots__ = ()

    role: type[ThreadRole]
'''
CHANNEL_SOURCES = '''"""Original channel membership and history reads, without live thread assembly."""

from abc import ABC, abstractmethod
from agent_comms.tagged_thread_source import TaggedThreadSource
from agent_comms.thread_role_source import ThreadRoleSource


class ChannelMemberSource(TaggedThreadSource, ThreadRoleSource):
    __slots__ = ()


class ChannelHistorySource(ABC):
    __slots__ = ()

    name: str
    tags: frozenset[str]

    @property
    @abstractmethod
    def history_targets(self) -> frozenset[str]: ...
'''


def main() -> int:
    label = sys.argv[1]
    # Retrieve all original consumers of the relocated declarations, including tests.
    selected = set(subprocess.check_output([
        "git", "grep", "-l", "-E", "RosterVisibility|DEFAULT_ROSTER", HEAD,
        "--", "*.py",
    ], cwd=ROOT, text=True).splitlines())
    relatives = {entry.removeprefix(HEAD + ":") for entry in selected}
    relatives.update(("src/agent_comms/__init__.py", "src/agent_comms/activity_clocks.py",
                      "src/agent_comms/channel_management.py", "src/agent_comms/messaging/channels.py",
                      "src/agent_comms/messaging/read_basis.py", "src/agent_comms/messaging/messages.py",
                      "src/agent_comms/registry/thread_management.py", "src/agent_comms/registry/threads.py",
                      "src/agent_comms/catalog_document.py", "src/agent_comms/recorded_time.py",
                      "src/agent_comms/registry/thread_identity.py", "src/agent_comms/task_owner.py",
                      "src/agent_comms/tagged_thread_source.py", "src/agent_comms/thread_identity_source.py",
                      "src/agent_comms/registry_names.py"))
    sources = {str(ROOT / relative): subprocess.check_output([
        "git", "show", f"{HEAD}:{relative}"], cwd=ROOT, text=True)
        for relative in sorted(relatives)}
    snapshot = CodemodSourceSnapshot.from_source_mapping(sources, analysis_roots=(ROOT / "src", ROOT / "tests", ROOT / "tools"))
    census = []
    for source_path in sources:
        parsed = snapshot.parsed_module_for_source_path(source_path)
        census.extend({"file": source_path, "line": node.lineno, "name": node.name,
                       "direct": node in parsed.module.body}
                      for _, node in parsed.syntax_index.indexed_nodes_of_type(ast.ClassDef))
    (ARTIFACTS / f"{label}.census.json").write_text(json.dumps(census, indent=2) + "\n")

    def path(relative: str) -> str:
        return str(ROOT / "src/agent_comms" / relative)

    def patch(relative: str, pairs: tuple[tuple[str, str], ...]) -> dict[str, object]:
        return {"operation": "patch_target", "file_path": path(relative),
                "replacements": [{"old_source": old, "new_source": new} for old, new in pairs]}

    move = {"operation": "relocate_symbols_to_new_module",
            "file_path": path("registry/registry_document.py"),
            "symbol_qualnames": ["RosterVisibility", "DEFAULT_ROSTER"],
            "destination_path": path("registry/roster_visibility.py")}
    changes = [
        patch("activity_clocks.py", (
            ("from typing import TYPE_CHECKING, Self", "from typing import Self"),
            ("if TYPE_CHECKING:\n    from agent_comms.messaging.channels import Channel\n    from agent_comms.messaging.messages import Message\n", "from agent_comms.messaging.messages import Message\n"),
            ("    @classmethod\n    def for_views(cls, channels: Mapping[str, Channel],\n                  by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:\n        return {name: cls.combine(channel.history_scope().target_values(by_target))\n                for name, channel in channels.items()}\n", ""),
            ("from collections.abc import Iterable, Mapping", "from collections.abc import Iterable"),
        )),
        patch("messaging/channels.py", (
            ("from collections.abc import Iterable, Iterator", "from collections.abc import Iterable, Iterator, Mapping"),
            ("from typing import Annotated, ClassVar, TYPE_CHECKING", "from typing import Annotated, ClassVar"),
            ("if TYPE_CHECKING:\n    from agent_comms.registry.roster_visibility import RosterVisibility\n    from agent_comms.registry.threads import Thread\n    from agent_comms.registry_provenance import RegistryProvenance\n", "from agent_comms.registry.roster_visibility import RosterVisibility\nfrom agent_comms.registry.threads import Thread\nfrom agent_comms.registry_provenance import RegistryProvenance\nfrom agent_comms.activity_clocks import ChannelActivity\n"),
            ("    def pending_count(self, counts: dict[str, int]) -> int:\n", "    def history_activity(self, by_target: Mapping[str, ChannelActivity]) -> ChannelActivity:\n        return ChannelActivity.combine(self.history_scope().target_values(by_target))\n\n    def pending_count(self, counts: dict[str, int]) -> int:\n"),
            ("class Channels(dict[str, Channel]):\n    \"\"\"The captured declarations select the channels shown by a roster.\"\"\"\n", "class Channels(dict[str, Channel]):\n    \"\"\"The captured declarations select the channels shown by a roster.\"\"\"\n\n    def history_activity(self, by_target: Mapping[str, ChannelActivity]) -> Mapping[str, ChannelActivity]:\n        return {name: channel.history_activity(by_target) for name, channel in self.items()}\n"),
        )),
        patch("history_views.py", (
            ("ChannelActivity.for_views(channels, clocks.channels)", "channels.history_activity(clocks.channels)"),
            ("ChannelActivity.for_views(\n                {channel.name: channel}, clocks.channels)[channel.name]", "channel.history_activity(clocks.channels)"),
        )),
        patch("registry/thread_status.py", (
            ("from agent_comms.registry.thread_reads import ThreadOwnerStatus\n", "from agent_comms.registry.thread_reads import ThreadOwnerStatus\nfrom agent_comms.registry.roster_visibility import RosterVisibility\n"),
            ("    from agent_comms.registry.roster_visibility import RosterVisibility\n", ""),
        )),
        {"operation": "patch_target", "file_path": str(ROOT / "tests/test_thread_status.py"),
         "replacements": [
             {"old_source": "from agent_comms.registry.roster_visibility import DEFAULT_ROSTER\n",
              "new_source": "from agent_comms.registry.roster_visibility import DEFAULT_ROSTER, RosterVisibility\n"},
             {"old_source": "        else:\n            from agent_comms.registry.roster_visibility import RosterVisibility\n            if expected.in_view(",
              "new_source": "        else:\n            if expected.in_view("},
             {"old_source": "    from agent_comms.registry.roster_visibility import RosterVisibility\n\n    assert status.in_view",
              "new_source": "    assert status.in_view"},
         ]},
        patch("channel_management.py", (
            ("from typing import TYPE_CHECKING, ClassVar", "from typing import ClassVar"),
            ("from agent_comms.registry.thread_management import ChangedTags, GivenTags", "from agent_comms.registry.thread_management import ChangedTags, GivenTags, TagChoice, ThreadManagement"),
            ("if TYPE_CHECKING:\n    from agent_comms.catalog_document import CatalogDocument\n    from agent_comms.registry.thread_management import TagChoice, ThreadManagement\n", "from agent_comms.catalog_document import CatalogDocument\n"),
        )),
    ]
    reason = ("P23 guarded-import closure at Core " + HEAD + ": relocate the original roster policy and its default together below their consumers. "
              "Move channel target selection to Channel/Channels; preserve ChannelActivity aggregation, concrete wire field types, original member filtering and all service bodies. "
              "The appended cold-check correction puts the original clock read contract below destination/persistence initialization on MessageInput and ThreadRole through native parent operations. "
              "Bind original callback facts through their low nominal parents, preserving concrete wire fields; generic live channel callbacks retain their original member type. No surrogate records, new registries or namespace injection. "
              "This sequence is a source/preflight proposal, not executed import, codec or consumer evidence. Deferred ClassFact writes are unchanged.")
    plan = {"stages": [{"recipes": [{"recipe_id": identity, "reason": reason,
                                     "operations": operations, "architecture_guards": [], "authority_claims": []}],
                        "architecture_guards": []}
                       for identity, operations in (("p23-lower-original-roster-policy", [move]),
                                                    ("p23-close-channel-clock-callback-dependencies", changes))]}
    # Preserve the delivered prefix, then append the correction the cold check requires.
    # Only the original role's abstract declaration moves; concrete implementations stay put.
    correction = [
        {"recipes": [{"recipe_id": "p23-declare-low-original-clock-reads", "reason": reason,
                      "operations": [{"operation": "create_file", "file_path": path("message_clock_source.py"), "source": CLOCK_SOURCE}],
                      "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
        {"recipes": [{"recipe_id": "p23-bind-clock-read-parents", "reason": reason,
                      "operations": [
                          {"operation": "ensure_import", "file_path": path("messaging/messages.py"), "import_source": "from agent_comms.message_clock_source import MessageClockSource"},
                          {"operation": "ensure_import", "file_path": path("registry/thread_identity.py"), "import_source": "from agent_comms.message_clock_source import SenderClockSource"},
                      ], "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
        {"recipes": [{"recipe_id": "p23-remove-eager-message-clock-dependency", "reason": reason,
                      "operations": [
                          {"operation": "replace_class_base", "file_path": path("messaging/messages.py"), "target_qualname": "MessageInput", "base_name": "Timestamped", "replacement_base_name": "MessageClockSource"},
                          {"operation": "add_class_base", "file_path": path("registry/thread_identity.py"), "target_qualname": "ThreadRole", "base_name": "SenderClockSource"},
                          {"operation": "delete_target", "file_path": path("registry/thread_identity.py"), "target_qualname": "ThreadRole.last_user_input"},
                          patch("messaging/messages.py", (("from agent_comms.recorded_time import Timestamped\n", ""),)),
                          patch("activity_clocks.py", (
                              ("from agent_comms.messaging.messages import Message\n", "from agent_comms.message_clock_source import MessageClockSource\n"),
                              ("def observe(self, message: Message)", "def observe(self, message: MessageClockSource)"),
                              ("def from_message(cls, message: Message,", "def from_message(cls, message: MessageClockSource,"),
                          )),
                      ], "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
    ]
    def scoped_patch(relative: str, qualname: str, pairs: tuple[tuple[str, str], ...]) -> dict[str, object]:
        return dict(patch(relative, pairs), target_qualname=qualname)

    channel_body_changes = [
        {"operation": "add_class_base", "file_path": path("registry/threads.py"), "target_qualname": "Thread", "base_name": "ChannelMemberSource"},
        {"operation": "add_class_base", "file_path": path("task_owner.py"), "target_qualname": "TaskOwner", "base_name": "ThreadRoleSource"},
        {"operation": "add_class_base", "file_path": path("messaging/channels.py"), "target_qualname": "GroupedChannel", "base_name": "ChannelHistorySource"},
        {"operation": "delete_target", "file_path": path("messaging/channels.py"), "target_qualname": "GroupedChannel.history_targets"},
        patch("task_owner.py", (("from agent_comms.registry.thread_identity import ThreadRole, TurnFence, TurnIdentity", "from agent_comms.registry.thread_identity import TurnFence, TurnIdentity"),)),
        scoped_patch("task_owner.py", "TaskOwner", (("    role: type[ThreadRole]\n\n", ""),)),
        patch("messaging/channels.py", (
            ("from agent_comms.registry.threads import Thread\n", ""),
            ("from agent_comms.registry_provenance import RegistryProvenance\n", ""),
        )),
        scoped_patch("messaging/channels.py", "ChannelMemberIndex", (
            ('class ChannelMemberIndex(dict[str, tuple["Thread", ...]]):', 'class ChannelMemberIndex[Member: ChannelMemberSource](dict[str, tuple[Member, ...]]):'),
            ("threads: Iterable[Thread]", "threads: Iterable[Member]"),
            ("by_tag: dict[str, list[Thread]]", "by_tag: dict[str, list[Member]]"),
            ("def __missing__(self, tag: str) -> tuple[Thread, ...]", "def __missing__(self, tag: str) -> tuple[Member, ...]"),
        )),
        patch("messaging/read_basis.py", (("    from agent_comms.messaging.channels import TagChannel\n", ""),)),
        scoped_patch("messaging/read_basis.py", "MemberActivityDisplayScope.capture", (
            ("channel: TagChannel", "channel: ChannelHistorySource"),
            ("snapshot: RegistryProvenance", "snapshot: RegistryNames[TaggedThreadSource]"),
        )),
    ]
    # Derive affected callback owners from the original native census, not a second class roster.
    parsed_channels = snapshot.parsed_module_for_source_path(path("messaging/channels.py"))
    for _, owner in parsed_channels.syntax_index.indexed_nodes_of_type(ast.ClassDef):
        if owner not in parsed_channels.module.body:
            continue
        for member in parsed_channels.syntax_index.named_scope_children[owner]:
            if member not in owner.body:
                continue
            if member.name == "members":
                channel_body_changes.append(scoped_patch("messaging/channels.py", f"{owner.name}.members", (
                    ("def members(self, index: ChannelMemberIndex) -> tuple[Thread, ...]", "def members[Member: ChannelMemberSource](self, index: ChannelMemberIndex[Member]) -> tuple[Member, ...]"),
                )))
            if member.name == "display_scope":
                channel_body_changes.append(scoped_patch("messaging/channels.py", f"{owner.name}.display_scope", (
                    ("snapshot: RegistryProvenance", "snapshot: RegistryNames[TaggedThreadSource]"),
                )))
    channel_correction = [
        {"recipes": [{"recipe_id": "p23-lower-original-channel-read-owners", "reason": reason,
                      "operations": [
                          {"operation": "create_file", "file_path": path("thread_role_source.py"), "source": THREAD_ROLE_SOURCE},
                          {"operation": "create_file", "file_path": path("channel_sources.py"), "source": CHANNEL_SOURCES},
                      ], "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
        {"recipes": [{"recipe_id": "p23-bind-channel-read-parents", "reason": reason,
                      "operations": [
                          {"operation": "ensure_import", "file_path": path("task_owner.py"), "import_source": "from agent_comms.thread_role_source import ThreadRoleSource"},
                          {"operation": "ensure_import", "file_path": path("registry/threads.py"), "import_source": "from agent_comms.channel_sources import ChannelMemberSource"},
                          {"operation": "ensure_import", "file_path": path("messaging/channels.py"), "import_source": "from agent_comms.channel_sources import ChannelMemberSource, ChannelHistorySource"},
                          {"operation": "ensure_import", "file_path": path("messaging/channels.py"), "import_source": "from agent_comms.registry_names import RegistryNames"},
                          {"operation": "ensure_import", "file_path": path("messaging/channels.py"), "import_source": "from agent_comms.tagged_thread_source import TaggedThreadSource"},
                          {"operation": "ensure_import", "file_path": path("messaging/read_basis.py"), "import_source": "from agent_comms.channel_sources import ChannelHistorySource"},
                          {"operation": "ensure_import", "file_path": path("messaging/read_basis.py"), "import_source": "from agent_comms.registry_names import RegistryNames"},
                          {"operation": "ensure_import", "file_path": path("messaging/read_basis.py"), "import_source": "from agent_comms.tagged_thread_source import TaggedThreadSource"},
                      ], "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
        {"recipes": [{"recipe_id": "p23-remove-channel-thread-bootstrap", "reason": reason,
                      "operations": channel_body_changes, "architecture_guards": [], "authority_claims": []}], "architecture_guards": []},
    ]
    (ARTIFACTS / f"{label}.channel-correction.plan.json").write_text(json.dumps({"stages": channel_correction}, indent=2) + "\n")
    correction.extend(channel_correction)
    plan["stages"].extend(correction)
    (ARTIFACTS / f"{label}.correction.plan.json").write_text(json.dumps({"stages": correction}, indent=2) + "\n")
    (ARTIFACTS / f"{label}.plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    print(f"Native simulation on {len(sources)} pinned inputs; no apply path.", flush=True)
    try:
        result = CodemodPlanRoot.from_json_value(plan).as_sequence().simulate(snapshot)
    except CodemodOperationPreflightError as error:
        (ARTIFACTS / f"{label}.refusal.json").write_text(json.dumps(json_report_object(error.report), indent=2) + "\n")
        raise
    (ARTIFACTS / f"{label}.simulation.json").write_text(json.dumps(result.execution_payload(), indent=2) + "\n")
    projected = result.final_snapshot.sources_by_file_path
    diff = "".join(line for source_path in sorted(set(sources) | set(projected))
                   if sources.get(source_path) != projected.get(source_path)
                   for line in difflib.unified_diff(
                       sources.get(source_path, "").splitlines(keepends=True),
                       projected.get(source_path, "").splitlines(keepends=True),
                       fromfile=str(Path(source_path).relative_to(ROOT)),
                       tofile=str(Path(source_path).relative_to(ROOT))))
    (ARTIFACTS / f"{label}.diff").write_text(diff)
    print(f"Native execution report: {ARTIFACTS / (label + '.simulation.json')}", flush=True)
    return 0 if result.is_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
