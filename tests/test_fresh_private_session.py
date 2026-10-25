import json
from pathlib import Path

from agent_comms.fresh_private_session import create_fresh_private_session
from agent_comms.native_entries import NativeEntry, SessionEntry


def test_fresh_session_creates_unique_private_sdk_headers(tmp_path: Path) -> None:
    first = create_fresh_private_session(tmp_path, worktree=tmp_path)
    second = create_fresh_private_session(tmp_path, worktree=tmp_path)
    directory = first.path.parent
    assert first != second and first.path != second.path
    for session in (first, second):
        encoded = session.path.read_bytes()
        assert encoded.endswith(b"\n") and len(encoded.splitlines()) == 1
        header = NativeEntry.from_wire(json.loads(encoded))
        assert isinstance(header, SessionEntry)
        assert (header.id, header.version, header.cwd.text_or('')) == (session.session_id, 3, str(tmp_path))
        assert session.path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
