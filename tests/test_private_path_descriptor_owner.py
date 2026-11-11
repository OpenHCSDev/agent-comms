"""A filesystem role never hands out a descriptor nobody owns."""

from __future__ import annotations

import os
import resource
from pathlib import Path

import pytest

from agent_comms.errors import RelationViolationError
from agent_comms.storage.private_path import FilesystemRole, PrivateFileRole


def open_descriptors() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


@pytest.fixture
def private_file(tmp_path: Path) -> Path:
    path = tmp_path / "witness"
    path.write_bytes(b"body")
    path.chmod(0o600)
    return path


def test_reading_closes_its_descriptor_when_the_block_ends(private_file: Path) -> None:
    before = open_descriptors()
    with PrivateFileRole.reading(private_file, ValueError("not private")) as fd:
        assert os.read(fd, 4) == b"body"
        assert open_descriptors() == before + 1
    assert open_descriptors() == before


def test_reading_closes_its_descriptor_when_the_block_raises(private_file: Path) -> None:
    before = open_descriptors()
    with pytest.raises(RuntimeError):
        with PrivateFileRole.reading(private_file, ValueError("not private")):
            raise RuntimeError("the body failed")
    assert open_descriptors() == before


def test_a_refused_role_leaves_nothing_open(tmp_path: Path) -> None:
    """The check runs on an open descriptor, so a refusal must close it too."""
    public = tmp_path / "public"
    public.write_bytes(b"body")
    public.chmod(0o644)
    before = open_descriptors()
    with pytest.raises(ValueError):
        with PrivateFileRole.reading(public, ValueError("not private")):
            pytest.fail("a public file must not be admitted as private")
    assert open_descriptors() == before


def test_a_stream_owns_the_descriptor_it_was_given(private_file: Path) -> None:
    """Holders that outlive a scope take the stream, and closing it closes the file."""
    before = open_descriptors()
    source = PrivateFileRole.stream(private_file, ValueError("not private"))
    assert open_descriptors() == before + 1
    assert source.read() == b"body"
    source.close()
    assert open_descriptors() == before


def test_creating_closes_its_descriptor(tmp_path: Path) -> None:
    before = open_descriptors()
    with PrivateFileRole.creating(tmp_path / "fresh", ValueError("not private")) as fd:
        os.write(fd, b"body")
    assert open_descriptors() == before
    assert (tmp_path / "fresh").read_bytes() == b"body"


def test_no_role_hands_back_a_bare_descriptor() -> None:
    """The lifetime has one owner, so nothing public may return an int again.

    A role that returns an int makes its own close a fact every caller restates, and a
    caller that forgets holds the file for the life of its process. This is the guard
    that keeps the next one from being written: the public openers are the scope and the
    stream, and `_checked` stays private to them.
    """
    public = {
        name for name in vars(FilesystemRole) | vars(PrivateFileRole)
        if not name.startswith("_")
    }
    assert "read" not in public, "a role must not return a bare descriptor again"
    assert "update" not in public
    assert "create" not in public
    assert {"opened", "reading", "updating", "stream"} <= public


def test_many_reads_do_not_accumulate_descriptors(private_file: Path) -> None:
    """The failure this change exists for: a host that read in a loop ran out of files."""
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    before = open_descriptors()
    for _ in range(min(soft, 400)):
        with PrivateFileRole.reading(private_file, RelationViolationError("not private")) as fd:
            os.fstat(fd)
    assert open_descriptors() == before
