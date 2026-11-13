"""One registry answer turns wanted text into a free thread name."""
from conftest import ui_core_read
import os
from pathlib import Path

import pytest

from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.execution.child_process import ProcessIdentity
from agent_comms.projects.checkouts import Checkout
from agent_comms.registry.thread_management import ThreadTitleResult
from agent_comms.registry.threads import NativeThread
from agent_comms.transport.tool_caller import ThreadCaller


def _declare(comms, name, worktree):
    comms.registry.declare(NativeThread(
        name, frozenset(), Checkout(Path(worktree)), process_identity=ProcessIdentity.capture(os.getpid())))


def test_wanted_text_gets_one_slug_and_the_first_free_suffix(tmp_path):
    comms = wire(tmp_path / "wire")
    names = comms.registry.snapshot()
    # Session folders and rename titles share one slug rule.
    assert names.claim_name("My project!") == "My-project"
    assert names.claim_name("_scratch_") == "_scratch_"
    assert names.claim_name("***") == "session"
    _declare(comms, "user", str(tmp_path))
    _declare(comms, "user-2", str(tmp_path))
    assert comms.registry.snapshot().claim_name("user") == "user-3"


def test_a_permanent_alias_is_reserved_except_for_its_own_thread(tmp_path):
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    names = comms.registry.snapshot()
    assert names.taken("worker") and names.taken("renamed")
    assert names.claim_name("worker") == "worker-2"
    # A rename back to one of the thread's own names keeps that name.
    assert names.claim_name("worker", owner="renamed") == "worker"
    assert names.claim_name("renamed", owner="renamed") == "renamed"


def test_rename_takes_its_own_alias_and_refuses_another_threads(tmp_path):
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    _declare(comms, "other", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    names = comms.registry.snapshot()
    assert names.taken("worker") and not names.taken("worker", owner="renamed")
    with pytest.raises(RelationViolationError):
        comms.threads.rename_self("worker", caller=ThreadCaller("other"))
    assert comms.threads.rename_self("worker", caller=ThreadCaller("renamed")).current == "worker"
    assert comms.registry.canonical_name("renamed") == "worker"


def test_a_thread_corrects_its_own_title_without_renaming(tmp_path):
    """The label is writable on its own, so an imported title is never frozen.

    Nothing else could do this: a rename refuses another thread's name but this takes
    only the caller's own authority, which is the party that can see the stale label.
    """
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    assert comms.threads.set_own_title(
        "A corrected label", caller=ThreadCaller("renamed")) == ThreadTitleResult(
            "renamed", "A corrected label")
    thread = comms.registry.require("renamed")
    assert thread.name == "renamed" and thread.display_title == "A corrected label"
    assert comms.registry.canonical_name("worker") == "renamed"
    with pytest.raises(ValueError):
        comms.threads.set_own_title(
            "one two three four five six seven", caller=ThreadCaller("renamed"))
    assert comms.registry.require("renamed").display_title == "A corrected label"


def test_a_remote_ui_reads_the_registry_with_its_renames(tmp_path):
    """The registry Core answers survives the wire: a rename's alias still resolves remotely."""
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.transport.tools.sidebar import CommsRegistryTool

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    answer = ui_core_read(comms, CommsRegistryTool())
    received = FieldCodec.decode(RegistrySnapshot, FieldCodec.encode(answer))
    assert received == comms.registry.snapshot()
    assert received.canonical_name("worker") == "renamed"
    # Core's host selects by the project path its registry records, following its aliases.
    project = tmp_path.resolve()
    assert comms.registry.session_thread(project, "worker").name_or("worker") == "renamed"
    assert comms.registry.session_thread(project / "elsewhere", "worker").name_or("kept") == "kept"
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(project)
    assert comms.registry.session_thread(alias, "worker").name_or("worker") == "renamed"


def test_a_ui_reads_whether_a_channel_takes_input_through_core(tmp_path):
    """A channel answers as its own family member across the codec, so a remote composer reads
    the same read_only a local one does."""
    from agent_comms.messaging.channels import Channel
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tools.sidebar import CommsChannelTool

    comms = wire(tmp_path / "wire")
    for name in ("#all", "#any"):
        answer = ui_core_read(comms, CommsChannelTool(name=name))
        assert FieldCodec.decode(Channel, FieldCodec.encode(answer)) == answer
        assert answer.read_only is False


def test_a_ui_session_finds_its_project_thread_through_core(tmp_path):
    """Core resolves the project path on its own host, and the selection arrives as itself."""
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import (
        FoundProjectThread,
        NoProjectThread,
        ProjectThreadSelection,
    )
    from agent_comms.transport.tools.sidebar import CommsSessionThreadTool

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(tmp_path.resolve())
    for project, expected in ((alias, FoundProjectThread), (tmp_path / "elsewhere", NoProjectThread)):
        answer = ui_core_read(comms, CommsSessionThreadTool(project=str(project), preferred="worker"))
        received = FieldCodec.decode(ProjectThreadSelection, FieldCodec.encode(answer))
        assert received == answer and isinstance(received, expected)
    assert received.name_or("kept") == "kept"


def test_a_ui_reads_a_threads_configuration_through_core(tmp_path):
    """The status a DM shows is Core's configuration answer, the same across the codec."""
    from agent_comms.acp_extension import CoordinationChangedUpdate
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tools.sidebar import CommsThreadConfigurationTool

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    answer = ui_core_read(comms, CommsThreadConfigurationTool(thread="worker"))
    received = FieldCodec.decode(CoordinationChangedUpdate, FieldCodec.encode(answer))
    assert received.status_label == comms.configuration.answer("worker").status_label


def test_a_ui_asks_core_which_open_views_lost_their_source(tmp_path):
    """A deleted thread and an unknown channel are retired; the answer crosses the codec."""
    from agent_comms.history_views import RetiredViews
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tools.sidebar import CommsRetiredViewsTool

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    _declare(comms, "gone", str(tmp_path))
    kept, deleted = (comms.registry.require(name).incarnation for name in ("worker", "gone"))
    comms.registry.remove("gone")
    request = CommsRetiredViewsTool(threads=frozenset({kept, deleted}), channels=frozenset({"#all", "#nowhere"}))
    answer = ui_core_read(comms, FieldCodec.decode(CommsRetiredViewsTool, FieldCodec.encode(request)))
    received = FieldCodec.decode(RetiredViews, FieldCodec.encode(answer))
    assert received.threads == {deleted}
    assert received.channels == {"#nowhere"}


def test_a_received_registry_answers_membership_under_its_aliases(tmp_path):
    """A UI holding Core's registry snapshot asks membership the way Core's registration does."""
    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    snapshot = comms.registry.snapshot()
    assert "renamed" in snapshot and "worker" in snapshot and "nobody" not in snapshot
    assert ("worker" in comms.registry) == ("worker" in snapshot)


def test_a_ui_reads_and_orders_relationships_through_core(tmp_path):
    """A thread sidebar's relationship reads and its ordering write cross as themselves."""
    from agent_comms.display_order import ThreadSort
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.presentation import WireRevision
    from agent_comms.registry.relationships import ChildrenRelationshipGroup, ThreadCommsSnapshot
    from agent_comms.transport.tools.sidebar import (
        CommsSetRelationshipOrderTool,
        CommsThreadRelationshipsTool,
        CommsWireRevisionTool,
        RelationshipOrdered,
    )

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    # one child per read; see ui_core_read
    revision = ui_core_read(comms, CommsWireRevisionTool())
    assert FieldCodec.decode(WireRevision, FieldCodec.encode(revision)) == revision
    read = ui_core_read(comms, CommsThreadRelationshipsTool(owner="worker"))
    assert FieldCodec.decode(ThreadCommsSnapshot, FieldCodec.encode(read)) == read
    assert read == comms.relationships.snapshot("worker")
    order = ThreadSort.members_with(ThreadSort)[-1]
    ordered = ui_core_read(comms, CommsSetRelationshipOrderTool(owner="worker", group=ChildrenRelationshipGroup, order=order))
    assert FieldCodec.decode(RelationshipOrdered, FieldCodec.encode(ordered)) == ordered


def test_a_queue_action_request_crosses_as_itself():
    """A remote UI names the original recipient and the action; Core reaches the owner."""
    from agent_comms.nominal.field_codec import FieldCodec, ThreadLookup
    from agent_comms.transport.tools.sidebar import (
        ClearQueueQueueAction,
        CommsQueueActionTool,
        SendNowQueueAction,
    )

    lookup = ThreadLookup.mint()
    for action in (SendNowQueueAction, ClearQueueQueueAction):
        request = CommsQueueActionTool(lookup=lookup, action=action)
        assert FieldCodec.decode(CommsQueueActionTool, FieldCodec.encode(request)) == request


def test_a_ui_reads_a_threads_context_through_core(tmp_path):
    """A context explorer's reads cross as themselves; the owner and saved reads are declared."""
    import agent_comms.transport.runtime_requests as runtime
    from agent_comms.context_store import (
        BeforeContextManifest,
        ContextManifestCursor,
    )
    from agent_comms.execution.context_prior import ContextStoreIdentity
    from agent_comms.nominal.field_codec import ContextStoreId, FieldCodec
    from agent_comms.transport.tools.sidebar import (
        CommsContextInspectionTool,
        CommsOwnerRequestTool,
        ContextInspectionRead,
    )

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    read = ui_core_read(comms, CommsContextInspectionTool(owner="worker"))
    assert FieldCodec.decode(ContextInspectionRead, FieldCodec.encode(read)) == read
    assert read.entry.thread.name == "worker"
    cursor = BeforeContextManifest(ContextStoreIdentity(ContextStoreId.mint()), 4)
    assert FieldCodec.decode(ContextManifestCursor, FieldCodec.encode(cursor, ContextManifestCursor)) == cursor
    request = CommsOwnerRequestTool(owner=read.entry.thread.incarnation,
                                    request=runtime.ContextCoreRuntimeRequest(thread="worker"))
    assert FieldCodec.decode(CommsOwnerRequestTool, FieldCodec.encode(request)) == request


def test_a_ui_lists_historical_identities_through_core(tmp_path):
    """Every sender is null and one sender is its name, so neither needs a None flag."""
    from agent_comms.messaging.historical_views import (
        AllHistoricalSenders,
        HistoricalSenders,
        NamedHistoricalSender,
    )
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.transport.tools.sidebar import (
        CommsHistoricalThreadsTool,
        HistoricalThreadsRead,
    )

    assert FieldCodec.encode(AllHistoricalSenders(), HistoricalSenders) is None
    assert FieldCodec.decode(HistoricalSenders, "alice") == NamedHistoricalSender("alice")
    comms = wire(tmp_path / "wire")
    for senders in (AllHistoricalSenders(), NamedHistoricalSender("alice")):
        read = ui_core_read(comms, CommsHistoricalThreadsTool(senders=senders))
        assert FieldCodec.decode(HistoricalThreadsRead, FieldCodec.encode(read)) == read


def test_a_ui_reads_a_renamed_thread_from_a_registry_slice(tmp_path):
    """The slice a remote UI reads answers under the old name and carries no other thread."""
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry.registry_document import RegistrySlice
    from agent_comms.transport.tools.sidebar import CommsRegistrySliceTool

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    _declare(comms, "bystander", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    request = CommsRegistrySliceTool(threads=("worker",))
    answer = ui_core_read(comms, FieldCodec.decode(CommsRegistrySliceTool, FieldCodec.encode(request)))
    cut = FieldCodec.decode(RegistrySlice, FieldCodec.encode(answer))

    assert cut.canonical_name("worker") == "renamed"
    assert cut.require("worker").incarnation == comms.registry.snapshot().require("renamed").incarnation
    assert "bystander" not in cut
