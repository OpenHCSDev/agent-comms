"""One registry answer turns wanted text into a free thread name."""
from pathlib import Path

from agent_comms.repo import Checkout
import os

import pytest

from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.errors import RelationViolationError
from agent_comms.threads import Thread


def _declare(comms, name, worktree):
    comms.registry.declare(Thread(
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
        comms.threads._rename_thread("other", "worker")
    assert comms.threads._rename_thread("renamed", "worker").current == "worker"
    assert comms.registry.canonical_name("renamed") == "worker"


def test_a_remote_ui_reads_the_registry_with_its_renames(tmp_path):
    """The registry Core answers survives the wire: a rename's alias still resolves remotely."""
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsRegistryTool, RegistryRead

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    comms.registry.rename("worker", "renamed")
    answer = LocalCoreAccess(comms).call(CommsRegistryTool())
    received = FieldCodec.decode(RegistryRead, FieldCodec.encode(answer))
    assert received.snapshot == comms.registry.snapshot()
    assert received.snapshot.canonical_name("worker") == "renamed"
    # The received snapshot selects by the project path Core records, compared exactly.
    project = tmp_path.resolve()
    assert received.snapshot.session_thread(project, "worker").name_or("worker") == "renamed"
    assert received.snapshot.session_thread(project / "elsewhere", "worker").name_or("kept") == "kept"
    # An alias resolves only on Core's host: a detached copy elsewhere never follows it.
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(project)
    assert received.snapshot.session_thread(alias, "worker").name_or("kept") == "kept"
    assert comms.registry.session_thread(alias, "worker").name_or("worker") == "renamed"


def test_a_ui_reads_whether_a_channel_takes_input_through_core(tmp_path):
    """A channel answers as its own family member across the codec, so a remote composer reads
    the same read_only a local one does."""
    from agent_comms.channels import Channel
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsChannelTool

    comms = wire(tmp_path / "wire")
    for name in ("#all", "#any"):
        answer = LocalCoreAccess(comms).call(CommsChannelTool(name=name))
        assert FieldCodec.decode(Channel, FieldCodec.encode(answer)) == answer
        assert answer.read_only is False


def test_a_ui_session_finds_its_project_thread_through_core(tmp_path):
    """Core resolves the project path on its own host, and the selection arrives as itself."""
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.registry_document import FoundProjectThread, NoProjectThread
    from agent_comms.tools import CommsSessionThreadTool, SessionThreadSelected

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    alias = tmp_path.parent / f"{tmp_path.name}-alias"
    alias.symlink_to(tmp_path.resolve())
    for project, expected in ((alias, FoundProjectThread), (tmp_path / "elsewhere", NoProjectThread)):
        answer = LocalCoreAccess(comms).call(CommsSessionThreadTool(project=str(project), preferred="worker"))
        received = FieldCodec.decode(SessionThreadSelected, FieldCodec.encode(answer))
        assert received == answer and isinstance(received.selection, expected)
    assert received.selection.name_or("kept") == "kept"


def test_a_ui_reads_a_threads_configuration_through_core(tmp_path):
    """The status a DM shows is Core's configuration answer, the same across the codec."""
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsThreadConfigurationTool, ThreadConfigured

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    answer = LocalCoreAccess(comms).call(CommsThreadConfigurationTool(thread="worker"))
    received = FieldCodec.decode(ThreadConfigured, FieldCodec.encode(answer))
    assert received.update.status_label == comms.configuration.answer("worker").status_label


def test_a_ui_asks_core_which_open_views_lost_their_source(tmp_path):
    """A deleted thread and an unknown channel are retired; the answer crosses the codec."""
    from agent_comms.core_access import LocalCoreAccess
    from agent_comms.nominal.field_codec import FieldCodec
    from agent_comms.tools import CommsRetiredViewsTool, RetiredViewsRead

    comms = wire(tmp_path / "wire")
    _declare(comms, "worker", str(tmp_path))
    _declare(comms, "gone", str(tmp_path))
    kept, deleted = (comms.registry.require(name).incarnation for name in ("worker", "gone"))
    comms.registry.remove("gone")
    request = CommsRetiredViewsTool(threads=frozenset({kept, deleted}), channels=frozenset({"#all", "#nowhere"}))
    answer = LocalCoreAccess(comms).call(FieldCodec.decode(CommsRetiredViewsTool, FieldCodec.encode(request)))
    received = FieldCodec.decode(RetiredViewsRead, FieldCodec.encode(answer))
    assert received.retired.threads == {deleted}
    assert received.retired.channels == {"#nowhere"}
