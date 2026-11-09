"""Credential carry controls use selected retained files, never the live HOME store."""

import os
import time
from contextlib import ExitStack
from pathlib import Path

import pytest
from cutover.carry_connector_access import (
    ConnectorAccessCarry,
    PreviousConnectorGrants,
    PreviousIssuedAccess,
    PreviousThreadGrant,
)

from agent_comms.errors import RelationViolationError
from agent_comms.installer.backup import StoreBackup
from agent_comms.installer.practice import PracticeCopy
from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord
from agent_comms.storage.locked_store import LiveFilesRequest, RetainedCopyRequest
from agent_comms.storage.private_path import PrivateDirectoryRole
from agent_comms.storage.store_files import AtomicFile, ExclusiveLock, store_lock_path
from agent_comms.transport.connector_caller import CALLER, ForkingCaller, GrantRefusedError
from agent_comms.transport.connector_store import (
    CALLBACK,
    AccessLabel,
    AuthorizationQuery,
    AuthorizationRequest,
    ConnectorAccess,
    ConnectorCredentialsFile,
    ConnectorGrants,
    ConnectorStore,
    ExistingDirectorAccess,
    IssuedCode,
    RegisteredClient,
    TrustedBrowser,
    digest,
)


@pytest.fixture
def previous_grants() -> PreviousConnectorGrants:
    expiry = time.time() + 3600
    query = AuthorizationQuery("code", "client", CALLBACK, "challenge", "S256", "owner-state")
    return PreviousConnectorGrants(
        clients={"client": RegisteredClient("Claude")},
        requests={query.request_id: AuthorizationRequest(
            query, digest("approval"), expires_at=expiry)},
        codes={digest("code"): IssuedCode("client", "challenge", expires_at=expiry)},
        access={
            digest("oauth"): PreviousIssuedAccess(AccessLabel("client"), expires_at=expiry),
            digest("service"): PreviousIssuedAccess(AccessLabel("local-proof"), expires_at=expiry),
            digest("expired"): PreviousIssuedAccess(AccessLabel("client"), expires_at=1),
        },
        threads={digest("thread"): PreviousThreadGrant("reader")},
        refresh={digest("refresh"): "client"},
        trusted_browsers={digest("browser"): TrustedBrowser(time.time())},
    )


@pytest.fixture
def selected_carry(tmp_path: Path) -> ConnectorAccessCarry:
    files = RetainedCopyRequest(tmp_path / "retained", tmp_path / "source")
    return ConnectorAccessCarry(files, tmp_path / "spill")


def selected_path(carry: ConnectorAccessCarry) -> Path:
    resource, = ConnectorCredentialsFile.resources(carry.files)
    return carry.files.location(resource)


def test_selected_credential_carry_preserves_live_authority_and_pending_state(
    selected_carry: ConnectorAccessCarry, previous_grants: PreviousConnectorGrants,
) -> None:
    path = selected_path(selected_carry)
    PrivateDirectoryRole.make_parents(path.parent)
    AtomicFile(path).write_text(FieldCodec.compact(FieldCodec.encode(previous_grants)))
    before = path.read_bytes()
    # The running reader accepts only the successor format, never a legacy fallback.
    with pytest.raises(MalformedRecord):
        ConnectorStore(path).read()
    proposed = selected_carry.carry(apply=False)
    assert proposed.changed == 1 and not proposed.rewritten
    assert path.read_bytes() == before
    applied = selected_carry.carry(apply=True)
    resource, = ConnectorCredentialsFile.resources(selected_carry.files)
    assert applied.changed == 1 and applied.rewritten == (resource.path,)
    current = ConnectorStore(path).read()
    assert current.clients == previous_grants.clients
    assert current.requests == previous_grants.requests
    assert current.codes == previous_grants.codes
    assert current.refresh == previous_grants.refresh
    assert current.trusted_browsers == previous_grants.trusted_browsers
    for key, old in previous_grants.access.items():
        assert current.access[key] == ExistingDirectorAccess(old.client_id, expires_at=old.expires_at)
    assert current.caller_for("oauth") == CALLER
    assert current.caller_for("service") == CALLER
    assert current.caller_for("thread") == ForkingCaller("reader")
    with pytest.raises(GrantRefusedError):
        current.caller_for("expired")
    assert current.thread_held("reader")
    successor = path.read_bytes()
    assert selected_carry.carry(apply=False).changed == 0
    assert selected_carry.carry(apply=True).changed == 0
    assert path.read_bytes() == successor


@pytest.mark.parametrize("apply", (False, True))
def test_colliding_legacy_bearer_digests_refuse_without_changing_bytes(
    selected_carry: ConnectorAccessCarry, apply: bool,
) -> None:
    key = digest("colliding")
    previous = PreviousConnectorGrants(
        access={key: PreviousIssuedAccess(AccessLabel("client"), expires_at=time.time() + 3600)},
        threads={key: PreviousThreadGrant("reader")},
    )
    path = selected_path(selected_carry)
    PrivateDirectoryRole.make_parents(path.parent)
    AtomicFile(path).write_text(FieldCodec.compact(FieldCodec.encode(previous)))
    before = path.read_bytes()
    with pytest.raises(RelationViolationError, match="share a bearer digest"):
        selected_carry.carry(apply=apply)
    assert path.read_bytes() == before


def test_unknown_current_authority_is_not_reinterpreted_as_legacy(
    selected_carry: ConnectorAccessCarry,
) -> None:
    path = selected_path(selected_carry)
    PrivateDirectoryRole.make_parents(path.parent)
    document = {
        FieldCodec.record_key(ConnectorGrants, "access"): {
            digest("foreign"): {ConnectorAccess.family_discriminator: "not_an_authority"},
        },
    }
    AtomicFile(path).write_text(FieldCodec.compact(document))
    before = path.read_bytes()
    for apply in (False, True):
        with pytest.raises(MalformedRecord):
            selected_carry.carry(apply=apply)
        assert path.read_bytes() == before


def test_no_selected_credential_file_requires_no_carry_or_creation(
    selected_carry: ConnectorAccessCarry,
) -> None:
    path = selected_path(selected_carry)
    for apply in (False, True):
        result = selected_carry.carry(apply=apply)
        assert result.changed == 0 and not result.rewritten
        assert not path.exists()


def test_credential_resource_refuses_an_aliased_retained_input(
    selected_carry: ConnectorAccessCarry, tmp_path: Path,
) -> None:
    path = selected_path(selected_carry)
    PrivateDirectoryRole.make_parents(path.parent)
    outside = tmp_path / "not-the-selected-file.json"
    AtomicFile(outside).write_text(FieldCodec.compact(FieldCodec.encode(ConnectorGrants())))
    path.symlink_to(outside)
    before = outside.read_bytes()
    with pytest.raises(RelationViolationError, match="unconfined path"):
        selected_carry.carry(apply=True)
    assert outside.read_bytes() == before


def test_live_selected_credentials_copy_carry_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous_grants: PreviousConnectorGrants,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = tmp_path / "bus"
    root.mkdir()
    path = ConnectorCredentialsFile.path()
    assert path.is_relative_to(home)
    PrivateDirectoryRole.make_parents(path.parent)
    AtomicFile(path).write_text(FieldCodec.compact(FieldCodec.encode(previous_grants)))
    original = path.read_bytes()
    step = ConnectorAccessCarry(LiveFilesRequest(root), tmp_path / "spill")
    copy = PracticeCopy(root, tmp_path / "practice")
    copy.include(step.input_files())
    copied_step = ConnectorAccessCarry(copy.request, tmp_path / "practice-spill")
    resource, = ConnectorCredentialsFile.resources(copy.request)
    assert copy.request.location(resource).read_bytes() == original
    assert copied_step.carry(apply=True).changed == 1
    assert path.read_bytes() == original
    with ExitStack() as lifetime:
        ConnectorCredentialsFile.borrowed_locks(lifetime, step.files)
        backup = StoreBackup.take(root, tmp_path / "route", step.input_files(), tmp_path / "backups")
        assert resource.path in backup.selected_files()
        assert backup.original()
        assert step.carry(apply=True).changed == 1
        assert ConnectorStore(path).read().clients == previous_grants.clients
        assert not backup.stores_original()
        backup.restore(lambda: FieldCodec.decode(PreviousConnectorGrants, FieldCodec.parse(path.read_text())))
        assert backup.original()
        assert path.read_bytes() == original


def test_selected_credential_writer_lock_lives_until_its_exit_stack_closes(
    selected_carry: ConnectorAccessCarry,
) -> None:
    path = selected_path(selected_carry)
    PrivateDirectoryRole.make_parents(path.parent)
    AtomicFile(path).write_text(FieldCodec.compact(FieldCodec.encode(ConnectorGrants())))
    before = path.read_bytes()
    with ExitStack() as lifetime:
        held, = ConnectorCredentialsFile.borrowed_locks(lifetime, selected_carry.files)
        assert held.path == path
        descriptor = os.open(store_lock_path(path), os.O_RDONLY)
        try:
            # A separate open description sees the real kernel lock, not this
            # thread's cooperative reentrant StoreGuard acquisition.
            with pytest.raises(BlockingIOError):
                ExclusiveLock().attempt(descriptor)
        finally:
            os.close(descriptor)
        assert path.read_bytes() == before
    descriptor = os.open(store_lock_path(path), os.O_RDONLY)
    try:
        ExclusiveLock().attempt(descriptor)
    finally:
        os.close(descriptor)
    assert path.read_bytes() == before
