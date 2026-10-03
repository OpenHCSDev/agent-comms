"""Catalogs through actual pinned Pi, without a provider prompt or saved input."""

import asyncio
import json
from dataclasses import replace

import pytest

from agent_comms import backend
from agent_comms.child_process import AttachedChild, BoundedRun
from agent_comms.comms import Comms
from agent_comms.config_options import ModelConfigOption, ThinkingLevelConfigOption
from agent_comms.native_pi import NativePiRpcLaunch
from agent_comms.pi_commands import SetModel, SetThinkingLevel
from agent_comms.pi_events import Response
from agent_comms.pi_rpc import PiRpcChannel
from agent_comms.threads import Thread
from delivery_owner_fixture import canonical_agent

pytest_plugins = ("test_backend_native_lifecycle",)


@pytest.fixture
async def catalog_owner(native_backend, monkeypatch):
    native = native_backend
    monkeypatch.delenv("AGENT_COMMS_AGENT_MODELS", raising=False)
    children = []
    start = AttachedChild.start

    async def observe(*args, **kwargs):
        child = await start(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(AttachedChild, "start", observe)
    owner = canonical_agent(
        Comms(native.root),
        agent_bin="pi",
        agent_args=["--model", "response-local/fixture", "--offline"],
        auto_wake=False,
    )
    monkeypatch.setattr(owner.inputs, "ensure_live_drain", lambda _: None)
    before = native.session.read_bytes()
    try:
        yield owner, children
    finally:
        await owner.shutdown()
        assert children and all(not child.alive() for child in children)
        assert native.provider.posts == 0
        assert native.session.read_bytes() == before
        assert native.saved_inputs() == []


async def test_actual_catalog_auth_refresh_preserves_selection_and_reaps_children(
    catalog_owner,
    native_backend,
):
    owner, children = catalog_owner
    native = native_backend
    config = owner.sessions.config
    session = await owner.new_session(str(native.project))
    thread = owner._comms.registry.require(session.session_id)
    model_catalog = config.catalog_for(ModelConfigOption)
    thinking_catalog = config.catalog_for(ThinkingLevelConfigOption)
    models = await model_catalog.choices(thread)
    assert "response-local/fixture" in {model.value for model in models}
    assert [choice.value for choice in await thinking_catalog.choices(thread)] == ["off"]
    assert len(children) == 2 and all(not child.alive() for child in children)
    # A cache hit does not launch another native process.
    assert await model_catalog.choices(thread) is models
    assert len(children) == 2
    auth = native.config / "auth.json"
    auth.write_text("{}\n")
    unavailable = await model_catalog.describe(thread)
    assert unavailable.current_value == "response-local/fixture"
    assert "response-local/fixture" in {choice.value for choice in unavailable.options}
    assert len(children) == 3
    assert owner._comms.registry.require(session.session_id).model == "response-local/fixture"
    # Restore this fixture's credential and add another local model. Discovery
    # must read actual updated config instead of an in-process mocked catalog.
    path = native.config / "models.json"
    document = json.loads(path.read_text())
    document["providers"]["response-local"]["models"].append(
        {"id": "second", "name": "Second local model", "reasoning": True}
    )
    path.write_text(json.dumps(document))
    auth.write_text(json.dumps({"response-local": {"type": "api_key", "key": "local-only"}}))
    refreshed = await model_catalog.choices(thread)
    assert {model.value for model in refreshed if model.value.startswith("response-local/")} == {
        "response-local/fixture",
        "response-local/second",
    }
    assert "medium" in {
        choice.value
        for choice in await thinking_catalog.choices(replace(thread, model="response-local/second"))
    }
    assert owner._comms.registry.require(session.session_id).model == "response-local/fixture"
    assert all(not child.alive() for child in children)
    print(f"actual_catalog_children_reaped={len(children)} provider_calls={native.provider.posts}")


async def test_actual_catalog_larger_than_stream_buffer(catalog_owner, native_backend):
    owner, children = catalog_owner
    path = native_backend.config / "models.json"
    document = json.loads(path.read_text())
    models = document["providers"]["response-local"]["models"]
    models.extend({"id": f"large-{i}-" + "x" * 2048, "name": str(i)} for i in range(70))
    path.write_text(json.dumps(document))
    thread = Thread(
        "catalog", frozenset(), str(native_backend.project), model="response-local/fixture"
    )
    result = await owner.sessions.config.catalog_for(ModelConfigOption).choices(thread)
    assert len([model for model in result if model.value.startswith("response-local/")]) == 71
    assert sum(len(model.value) for model in result) > 2 * 65536
    assert len(children) == 1 and not children[0].alive()
    print(f"actual_catalog_identifier_bytes={sum(len(model.value) for model in result)}")


async def test_actual_catalog_cancellation_reaps_child(catalog_owner, native_backend, monkeypatch):
    owner, children = catalog_owner
    reading = asyncio.Event()
    receive = PiRpcChannel.receive

    async def pause_read(channel, **options):
        reading.set()
        await asyncio.Event().wait()
        return await receive(channel, **options)

    monkeypatch.setattr(PiRpcChannel, "receive", pause_read)
    thread = Thread("catalog", frozenset(), str(native_backend.project))
    task = asyncio.create_task(owner.sessions.config.catalog_for(ModelConfigOption).choices(thread))
    try:
        await asyncio.wait_for(reading.wait(), 10)
        assert len(children) == 1 and children[0].alive()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not children[0].alive()


@pytest.mark.refactor_guard
def test_backend_no_longer_owns_discovery_or_catalog_projection():
    assert not {"discover_models", "discover_thinking_levels", "Model"}.intersection(vars(backend))
    assert not hasattr(PiRpcChannel, "request")


async def test_actual_native_setting_responses_use_the_command_result_owner(
    catalog_owner,
    native_backend,
):
    _owner, children = catalog_owner
    launch = NativePiRpcLaunch.managed(
        "pi",
        (
            "--model",
            "response-local/fixture",
            "--offline",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ),
        worktree=native_backend.project,
    )
    session = backend.TurnSession(launch, "")
    observed = []
    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=10) as child:
        channel = PiRpcChannel(child.stdout)
        for command, expected in (
            (SetModel(id="model", provider="response-local", model_id="fixture"), True),
            (SetThinkingLevel(id="thinking", level="off"), True),
            (SetModel(id="refused", provider="response-local", model_id="missing"), False),
        ):
            pending = channel.track(command)
            child.stdin.write(channel.command_bytes(command))
            await child.stdin.drain()
            while not pending.done():
                event = await channel.receive()
                assert event is not None
                if isinstance(event, Response):
                    channel.correlate(event)
            response = pending.result()
            updates = [event async for event in command.on_response(response, session)]
            assert len(updates) == 1 and isinstance(updates[0], command.result_type)
            assert updates[0].id == command.id and updates[0].ok is expected
            if not expected:
                assert response.error and updates[0].error == response.error
            observed.append((command.declared_name, updates[0].ok))
        assert not channel.pending._pending
    assert len(children) == 1 and not children[0].alive()
    print("actual_native_setting_results", observed)


@pytest.mark.parametrize("driver", ["immutable_cli", "sdk_callbacks"])
async def test_actual_native_replacement_callback_once_and_cancel(native_backend, monkeypatch, driver):
    """Actual SDK RPC replacement, extension hooks and retirement; no prompt."""
    from functools import partial
    from uuid import uuid4

    from agent_comms.coordinator import Coordination
    from agent_comms.native_custody import PiSessionChild
    import hashlib
    import os
    from pathlib import Path

    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.pi_commands import GetState, UnknownCommand

    native = native_backend
    source_name = os.environ.get("PI_REPLACEMENT_TEST_SOURCE")
    if not source_name:
        pytest.skip("Set PI_REPLACEMENT_TEST_SOURCE to an actual completed saved native session")
    source = Path(source_name).resolve(strict=True)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    created = await ForkSessionHelper.run(ForkSessionRequest(
        os.environ["PI_COMPACTION_TEST_PACKAGE"], str(source), str(native.project),
        directory=str(native.project / "replacement-sessions"),
    ), cwd=native.project)
    events = native.project / "replacement-events.jsonl"
    cancelled_target = native.project / "cancelled.jsonl"
    command = "pi"
    if driver == "sdk_callbacks":
        from native_event_host import install_event_host
        from urllib.parse import urlsplit

        address = urlsplit(json.loads((native.config / "models.json").read_text())["providers"]["response-local"]["baseUrl"])
        command = "sdk-replacement-probe"
        install_event_host(monkeypatch, command, f"{address.scheme}://{address.netloc}",
                           replacement_probe=events, cancelled_session=cancelled_target)
    launch = await Coordination.run_worker(partial(NativePiRpcLaunch.managed,
        command, ("--provider", "response-local", "--model", "fixture", "--thinking", "off",
               "--offline", "--no-extensions",
               "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-tools"),
        worktree=native.project, session_file=created.session_file,
    ))

    def observed():
        return [json.loads(row) for row in events.read_text().splitlines()]

    async with BoundedRun.session(launch.argv, cwd=launch.cwd, env=launch.env, timeout=30) as child:
        channel = PiRpcChannel(child.stdout)
        errors = asyncio.create_task(PiSessionChild.stderr_tail(child.stderr))

        async def state():
            request = GetState(id=uuid4().hex)
            try:
                response = await request.exchange(channel, child.stdin, strict=True)
            except EOFError as error:
                error.add_note(await errors)
                raise
            return response.require_request(request)

        async def replacement(frame):
            # These are vendor RPC frames, including fork's external entryId.
            # The original open-command representation and channel decode own
            # framing; this control creates no replacement pending registry.
            identity = uuid4().hex
            command = UnknownCommand(wire={"id": identity, **frame})
            child.stdin.write(channel.command_bytes(command))
            await child.stdin.drain()
            while True:
                event = await channel.receive(strict=True)
                assert event is not None, await errors
                if isinstance(event, Response) and event.id == identity:
                    assert event.success, event
                    return event.data.require_payload()

        initial = await state()
        if driver == "sdk_callbacks":
            assert len(observed()) == 1 and observed()[0]["sessionId"] == initial.session_id
        # Fork-before requires an actual user message, unlike clone-at. Ask
        # the active SDK owner, not the raw saved-file tail, for that operand.
        messages = await replacement({"type": "get_fork_messages"})
        entry = messages.payload["messages"][-1]["entryId"]
        for frame in (
            {"type": "switch_session", "sessionPath": created.session_file},
            {"type": "clone"},
            {"type": "fork", "entryId": entry},
            {"type": "new_session"},
        ):
            previous = await state()
            if driver == "sdk_callbacks":
                count = len(observed())
            await replacement(frame)
            current = await state()
            assert current.session_id
            if frame["type"] == "switch_session":
                assert current.identity == previous.identity
            else:
                assert current.session_id != previous.session_id
            if driver == "sdk_callbacks":
                assert len(observed()) == count + 1, (frame, observed())
                assert observed()[-1]["sessionId"] == current.session_id
        if driver == "sdk_callbacks":
            before = await state()
            count = len(observed())
            await replacement({"type": "switch_session", "sessionPath": str(cancelled_target)})
            after = await state()
            assert after.identity == before.identity and len(observed()) == count
            assert not cancelled_target.exists()
        assert native.provider.posts == 0 and native.saved_inputs() == []
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
        print("actual_native_replacement", driver, observed() if driver == "sdk_callbacks" else current.session_id, flush=True)
    assert child.returncode is not None and not child.alive()
    assert await errors == ""


async def test_actual_compaction_writer_borrows_acquired_native_launch(native_backend, monkeypatch):
    """Original SDK fork/get_state/writer resources; no input or provider call."""
    import hashlib
    import os
    from functools import partial
    from pathlib import Path

    from agent_comms import native_compaction_writer as writer
    from agent_comms.coordinator import Coordination
    from agent_comms.native_custody import PiSessionChild
    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.owner_compaction_commit import OwnerCompactionCommit

    native = native_backend
    source_name = os.environ.get("PI_WRITER_TEST_SOURCE")
    if not source_name:
        pytest.skip("Set PI_WRITER_TEST_SOURCE to an original saved native source")
    source = Path(source_name).resolve(strict=True)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    created = await ForkSessionHelper.run(ForkSessionRequest(
        os.environ["PI_COMPACTION_TEST_PACKAGE"], str(source), str(native.project),
        directory=str(native.project / "writer-sessions"),
    ), cwd=native.project)
    launch = await Coordination.run_worker(partial(NativePiRpcLaunch.managed,
        "pi", ("--provider", "response-local", "--model", "fixture", "--thinking", "off",
               "--offline", "--no-extensions", "--no-skills", "--no-context-files",
               "--no-prompt-templates", "--no-tools"),
        worktree=native.project, session_file=created.session_file,
    ))
    child = await PiSessionChild.start(
        (launch, launch.configuration.auth_revision()), launch.session.attestation(),
    )
    calls = []
    verify = writer.verify_native_package

    def observe(package):
        calls.append(package)
        return verify(package)

    monkeypatch.setattr(writer, "verify_native_package", observe)
    try:
        async with asyncio.timeout(30):
            pending = child.attestation
            response = await pending.request.exchange(child.reader, child.proc.stdin, strict=True)
            child.attestation = pending.accept(response)
            child.attestation.require_identity().require_same_session(created)
            async with OwnerCompactionCommit.open(
                native.root / "registry.json", launch.package, created.session_file,
                native_launch=launch,
            ) as bridge:
                assert bridge.native.package_dir == launch.package
                bridge.boundary.native_reader.require_path(created.path)
                assert calls == []
            # An unrelated path cannot borrow this acquired artifact; the check
            # occurs before helper access or journal creation.
            with pytest.raises(ValueError, match="differs from its acquired"):
                OwnerCompactionCommit(native.project / "registry.json", native.project,
                                      native_launch=launch)
            assert not (native.project / "compaction-commits.sqlite3").exists()
            await Coordination.run_worker(partial(writer.NativeCompactionWriter, launch.package))
            assert calls == [launch.package]
            response = await pending.request.exchange(child.reader, child.proc.stdin, strict=True)
            assert pending.accept(response).require_identity() == child.attestation.require_identity()
            assert native.provider.posts == 0 and native.saved_inputs() == []
            assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    finally:
        await child.close()
    assert not child.proc.alive() and child.proc.retired
    assert await child.stderr_task == ""
    print("actual_compaction_launch_borrow", json.dumps({
        "source_bytes": source.stat().st_size, "source_sha256": original,
        "fork": created.session_file, "pid": child.proc.pid,
        "child_retired": child.proc.retired, "borrowed_tree_verifications": 0,
        "fresh_tree_verifications": len(calls), "provider_requests": native.provider.posts,
    }), flush=True)


async def test_actual_saved_source_reservation_borrows_native_coverage(native_backend, monkeypatch):
    import hashlib
    import os
    from functools import partial
    from pathlib import Path

    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.coordinator import Coordination
    from agent_comms.native_entries import NativeEvidenceRead
    from agent_comms.native_fork import ForkSessionRequest
    from agent_comms.pi_summary_payloads import SelectedModel
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from selected_summary_cases import manual_summary_record

    native = native_backend
    name = os.environ.get("PI_WRITER_TEST_SOURCE")
    if not name:
        pytest.skip("Set PI_WRITER_TEST_SOURCE to an original saved native source")
    donor = Path(name).resolve(strict=True)
    before = hashlib.sha256(donor.read_bytes()).hexdigest()
    journal = CompactionJournal(native.root / "compaction-commits.sqlite3")
    created = await journal.private_inputs.fork(ForkSessionRequest(
        os.environ["PI_COMPACTION_TEST_PACKAGE"], str(donor), str(native.project),
        directory=str(native.root / "native-sessions"),
    ), cwd=native.project)
    source = manual_summary_record(created.path,
        selected=SelectedModel("response-local", "fixture", 2000000),
        settings=PiCompactionSettings(2048, 1))
    # The actual SDK creation is published in the journal, so continued-source
    # coverage must verify its original prefix; no invented enrollment/marker.
    with NativeEvidenceRead.open(created.path) as reader:
        reader.observe()
        decoded = []
        decode = reader.decode_rows

        def observe(rows):
            rows = tuple(rows)
            decoded.extend(rows)
            return decode(rows)

        monkeypatch.setattr(reader, "decode_rows", observe)
        operation = await Coordination.run_worker(partial(
            journal.summaries.reserve, created.session_file, source, native_reader=reader,
        ))
        assert decoded == [] and reader.entries
    assert journal.summaries.get(operation).request == source
    assert journal.summaries.blocking(created.session_file)
    assert native.provider.posts == 0 and native.saved_inputs() == []
    assert hashlib.sha256(donor.read_bytes()).hexdigest() == before
    print("actual_saved_prefix_reservation", json.dumps({
        "operation": operation, "source_bytes": donor.stat().st_size,
        "source_sha256": before, "redecoded_rows": len(decoded), "provider_requests": 0,
        "scope": "returned SDK fork/coverage reservation; no provider/commit/input authority",
    }), flush=True)
