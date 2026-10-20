"""Selected check history is small, non-executable and scoped to the command."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest

from agent_comms.check_root import CheckRoot
from agent_comms.cli import main
from agent_comms.comms import Comms
from agent_comms.external_transcript import ExternalTranscript
from agent_comms.transcript_events import AssistantTranscript, NoticeTranscript, UserTranscript
from agent_comms.native_entries import NativeEntry
from agent_comms.storage_rules import StorageRule
from agent_comms.thread_status import RunningThreadStatus, StoppedThreadStatus


@pytest.fixture
def scratch_root(tmp_path_factory):
    base = tmp_path_factory.getbasetemp()
    scratch = StorageRule().scratch_base.resolve()
    if not base.resolve().is_relative_to(scratch):
        base = scratch / "pytest"
        base.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="check-root-", dir=base) as directory:
        yield Path(directory)


@pytest.fixture
def source(scratch_root):
    root = scratch_root / "source"
    c = Comms(root)
    c.messaging.initialize_private_initial_protocol()
    for name in ("A", "B", "other"):
        c.registry.declare(c.registry.new_thread(name, frozenset({"comms"}), str(scratch_root)),
                           StoppedThreadStatus())
    c.messaging.send("A", "B", "old history is not a new input")
    c.registry.register(c.registry.require("other"), RunningThreadStatus())
    c.registry.rename("other", "unselected")
    return c




@pytest.mark.parametrize("timestamp", (None, 1_760_000_000.125))
def test_external_projection_preserves_event_clocks_or_refuses_unknown(source, scratch_root, timestamp):
    owner = source.registry.require("B")
    external = ExternalTranscript(source.root, owner.incarnation)
    events = (
        UserTranscript("source request " * 1500, position=1, timestamp=timestamp),
        AssistantTranscript("source reply", position=2,
                            timestamp=None if timestamp is None else timestamp + 1.125),
        NoticeTranscript("not portable conversation", position=3, timestamp=timestamp),
        UserTranscript("", position=4, timestamp=timestamp),
    )
    root = scratch_root / "external-window"
    if timestamp is None:
        # Current records require a clock at their storage boundary, before copying.
        with pytest.raises(TypeError):
            external.finish(turn_id="saved", item_id="item", events=events)
        with external.reading() as original:
            assert not original.has_records(original.cut().offset)
        assert not root.exists()
        return
    external.finish(turn_id="saved", item_id="item", events=events)
    with CheckRoot(source, (owner.name,), root).open() as copied:
        session = Path(copied.registry.require(owner.name).session_file)
        rows = [NativeEntry.read(line) for line in session.read_bytes().splitlines()]
        assert [entry.message.text for entry in rows[1:]] == [event.text for event in events[:2]]
        assert [entry.timestamp.at.timestamp() for entry in rows[1:]] == [
            event.timestamp for event in events[:2]
        ]
    with external.reading() as original:
        assert tuple(original.reverse(original.cut().offset))[0].events == events
    assert not root.exists()


def test_wrapped_nonzero_exit_removes_root_and_is_the_cli_exit(source, scratch_root, capsys):
    root = scratch_root / "command"
    status = main(["--root", str(source.root), "check-root", "--threads", "B", "--into", str(root),
                   "--", sys.executable, "-c",
                   "import pathlib,sys; assert pathlib.Path(sys.argv[1]).is_dir(); sys.exit(7)", "{root}"])
    assert status == 7
    assert json.loads(capsys.readouterr().out)["return_code"] == 7
    assert not root.exists()














