"""One acquired artifact through all owner launch consumers; trust stays strict."""
import dataclasses
from pathlib import Path

import pytest

from agent_comms import native_package
from agent_comms.native_arguments import NativeArguments, SelectedModel
from agent_comms.native_package import NativePackageError, VerifiedNativePackage
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.native_session_reopen import NativeSessionIdentity
from agent_comms.selected_session import SelectedSession
from agent_comms.selected_actions import NoSelectedTools


def test_acquired_artifact_owns_path_and_derived_cli(declared_native_package):
    with pytest.raises(TypeError, match="required positional argument: 'path'"):
        VerifiedNativePackage()
    verified = VerifiedNativePackage.acquire(declared_native_package)
    assert verified.path == declared_native_package
    assert verified.cli == declared_native_package / 'dist/cli.js'
    with pytest.raises(dataclasses.FrozenInstanceError):
        verified.path = Path('/changed')


def test_one_digest_for_managed_catalog_tracked_and_saved_header(declared_native_package, tmp_path, monkeypatch):
    calls = []
    digest = native_package.package_tree_digest

    def counted(root):
        calls.append(root)
        return digest(root)

    monkeypatch.setattr(native_package, 'package_tree_digest', counted)
    package = VerifiedNativePackage.acquire(declared_native_package)
    # This construction-only check has no installed worker launcher and starts no child.
    monkeypatch.setattr(NativeArguments, 'owner_launcher', classmethod(lambda cls, runtime: str(package.cli)))
    work = tmp_path / 'work'
    work.mkdir(mode=0o700)
    saved = work / 'saved.jsonl'
    saved.write_text('{"type":"session","id":"original","version":3,"timestamp":"2026-10-01T00:00:00.000Z"}\n')
    saved.chmod(0o600)
    identity = NativeSessionIdentity('original', str(saved))

    for _ in range(2):
        session = SelectedSession.for_launch(work, saved)
        assert session.identity == identity
        managed = NativePiRpcLaunch.managed('pi', (), package=package,
            worktree=work, root=tmp_path, session_file=str(saved), directory=work)
        catalog = NativePiRpcLaunch.catalog('pi', (), package=package, worktree=work)
        tracked = NativePiRpcLaunch.tracked(package, worktree=work, root=tmp_path,
            session=session, model=SelectedModel('local', 'fixture'),
            selected_tool_mode=NoSelectedTools())
        assert managed.package is catalog.package is tracked.package is package
        assert str(package.cli) in managed.argv
        assert str(package.cli) in catalog.argv
        assert str(package.cli) in tracked.argv
    assert calls == [declared_native_package]


@pytest.mark.parametrize('change', ['private-root', 'symlink-ancestor', 'dependency'])
def test_acquisition_preserves_launch_trust_checks(declared_native_package, tmp_path, change):
    if change == 'private-root':
        declared_native_package.parents[2].chmod(0o755)
        expected = 'owner-only'
        path = declared_native_package
    elif change == 'symlink-ancestor':
        link = tmp_path / 'redirected'
        link.symlink_to(declared_native_package.parents[2], target_is_directory=True)
        path = link / 'node_modules/@earendil-works/pi-coding-agent'
        expected = 'unsafe ancestor'
    else:
        (declared_native_package / 'dist/cli.js').write_text('// changed dependency\n')
        path = declared_native_package
        expected = 'differs from pinned'
    with pytest.raises(NativePackageError, match=expected):
        VerifiedNativePackage.acquire(path)
