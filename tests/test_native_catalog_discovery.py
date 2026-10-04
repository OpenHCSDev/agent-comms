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


async def test_actual_selected_launch_custody_rebuilds_source_and_auth(native_backend, monkeypatch):
    """Actual SDK fork/two fresh children; lend artifact, never input readiness."""
    import hashlib
    import os
    import threading
    from contextlib import AsyncExitStack
    from functools import partial
    from pathlib import Path

    from agent_comms import native_pi
    from agent_comms.coordinated_runtime import SelectedExecution
    from agent_comms.coordination_errors import IdentityConflict
    from agent_comms.coordinator import Coordination
    from agent_comms.native_custody import PiSessionChild
    from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
    from agent_comms.native_entries import NativeEntry
    from agent_comms.native_session_reopen import NativeSessionIdentity
    from agent_comms.selected_session import SavedSelectedSession

    source_name = os.environ.get("PI_LAUNCH_TEST_SOURCE")
    if not source_name:
        pytest.skip("Set PI_LAUNCH_TEST_SOURCE to an actual completed native source")
    native = native_backend
    source = Path(source_name).resolve(strict=True)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
    created = await ForkSessionHelper.run(ForkSessionRequest(
        str(package), str(source), str(native.project),
        directory=str(native.project / "launch-sessions"),
    ), cwd=native.project)
    session = SavedSelectedSession(created.path.parent,
        identity=NativeSessionIdentity(created.session_id, created.session_file))
    execution = SelectedExecution(root=native.root,
        wire_root_id=os.environ["AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID"],
        owner_name="launch-custody", native_package=package)
    verifications = []
    verify = native_pi._trusted_package

    def observed(artifact):
        verifications.append(artifact)
        return verify(artifact)

    monkeypatch.setattr(native_pi, "_trusted_package", observed)
    await Coordination.run_worker(execution.validate)
    launches, children = [], []
    for stage in range(2):
        if stage:
            # A new stage consumes current auth/config, not the old launch key.
            (native.config / "auth.json").write_text(json.dumps({
                "response-local": {"type": "api_key", "key": "changed-local-only"},
            }))
        launch = await Coordination.run_worker(partial(execution.tracked_launch,
            package, worktree=native.project, session=session,
            provider="response-local", model="fixture", thinking_level="off"))
        authentication = launch.configuration.auth_revision()
        launches.append((launch, authentication))
        child = await PiSessionChild.start((launch, authentication), session.attestation())
        children.append(child)
        try:
            async with asyncio.timeout(20):
                pending = child.attestation
                response = await pending.request.exchange(child.reader, child.proc.stdin, strict=True)
                pending.accept(response).require_identity().require_same_session(session.identity)
        finally:
            await child.close()
        assert child.proc.retired and await child.stderr_task == ""
    assert len(verifications) == 1  # both stages consume the pre-claim acquisition
    assert launches[0][1] != launches[1][1]
    assert children[0].proc.identity != children[1].proc.identity
    with pytest.raises(IdentityConflict, match="differs from its execution package"):
        await Coordination.run_worker(partial(execution.tracked_launch,
            package.parent,
            worktree=native.project, session=session,
            provider="response-local", model="fixture"))
    malformed = native.project / "malformed.jsonl"
    malformed.write_text("{}\n")
    wrong = SavedSelectedSession(malformed.parent,
        identity=NativeSessionIdentity(created.session_id, str(malformed)))
    with pytest.raises((ValueError, native_pi.NativePiUnavailable)):
        await Coordination.run_worker(partial(execution.tracked_launch,
            package, worktree=native.project, session=wrong,
            provider="response-local", model="fixture"))
    # Independent new acquisition still verifies the real artifact.
    await Coordination.run_worker(partial(NativePiRpcLaunch.tracked,
        package, worktree=native.project, session=session,
        provider="response-local", model="fixture"))
    assert len(verifications) == 2 and native.provider.posts == 0

    # The same installed worker joins a borrowed, actual SDK-source descriptor.
    # This checks read custody, not a synthetic input or provider commitment.
    entered, release = threading.Event(), threading.Event()
    readers = []

    async def observe_saved_source():
        async with AsyncExitStack() as custody:
            evidence = custody.enter_context(NativeEntry.open_input_evidence(session.path))
            readers.append(evidence)

            def observe():
                evidence.observe()
                entered.set()
                assert release.wait(5), "Original read was not released"
                assert not evidence.source.stream.closed

            await Coordination.run_worker(observe)

    observing = asyncio.create_task(observe_saved_source())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        for _ in range(2):
            observing.cancel()
            await asyncio.sleep(0)
            assert not observing.done() and not readers[0].source.stream.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await observing
        assert readers[0].source.stream.closed and not readers[0].entries
    finally:
        release.set()
        if not observing.done():
            observing.cancel()
        await asyncio.gather(observing, return_exceptions=True)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    print("actual_selected_launch_custody", json.dumps({
        "preclaim_and_first_verifications": 1, "borrowed_verifications": 0,
        "independent_verifications": 1, "auth_changed": True,
        "children_retired": [child.proc.retired for child in children],
        "children": [{"identity": {"pid": child.proc.identity.pid,
                                     "start_time": child.proc.identity.start_time},
                      "alive": child.proc.alive(),
                      "group_members": [member.pid for member in
                          child.proc.platform.group_members(child.proc.identity)]}
                     for child in children],
        "cancelled_saved_read_joined": readers[0].source.stream.closed,
        "source_bytes": source.stat().st_size, "source_sha256": before,
        "provider_requests": native.provider.posts,
    }), flush=True)


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


@pytest.mark.parametrize("continuation", ["next_input", "receipt_mismatch", "cancel_hook"])
async def test_known_compaction_retains_original_sdk_context_and_next_input(
    native_backend, monkeypatch, continuation
):
    """One real saved SDK fork -> fenced commit -> hook -> distinct input."""
    import hashlib
    import os
    from contextlib import aclosing
    from dataclasses import replace
    from pathlib import Path

    from agent_comms.child_process import ProcessIdentity
    from agent_comms.compaction_journal import CompactionJournal
    from agent_comms.compaction_records import CompactionOperation
    from agent_comms.goals import Goal
    from agent_comms.native_fork import ForkSessionRequest
    from agent_comms.native_session_prepare import NativeSessionPreparation
    from agent_comms.native_custody import ReopenNative
    from agent_comms.owner_compaction_commit import OwnerCompactionCommit
    from agent_comms.owner_compaction_provider import NativeSummary
    from agent_comms.owner_compaction_runtime import _commit_native_summary
    from agent_comms.owner_compaction_settings import PiCompactionSettings
    from agent_comms.pi_summary_payloads import SelectedModel
    from agent_comms.pi_vocabulary import ManualCompactionReason
    from agent_comms.pi_commands import AgentCommsRestoreCompaction
    from agent_comms.registration import Registration
    from agent_comms.selected_pi_route import prepare_selected_native_source
    from agent_comms.session_fence import session_writer_fence
    from agent_comms.threads import Thread
    from native_event_host import install_event_host
    from urllib.parse import urlsplit

    native = native_backend
    name = os.environ.get("PI_WRITER_TEST_SOURCE")
    if not name:
        pytest.skip("PI_WRITER_TEST_SOURCE must name original saved native evidence")
    donor = Path(name).resolve(strict=True)
    original = hashlib.sha256(donor.read_bytes()).hexdigest()
    created = await CompactionJournal(native.root / "compaction-commits.sqlite3").private_inputs.fork(
        ForkSessionRequest(os.environ["PI_COMPACTION_TEST_PACKAGE"], str(donor),
            str(native.project), directory=str(native.root / "native-sessions")),
        cwd=native.project,
    )
    native.session = created.path
    registry = Registration(native.root / "registry.json")
    registry.register(Thread("owner", frozenset(), str(native.project),
        process_identity=ProcessIdentity.capture(os.getpid()),
        session_file=created.session_file, model="response-local/fixture",
        goal=Goal("Keep original source and inputs", "known-commit-control")))
    owner, generation = registry.live_owner_with_generation("owner")
    owner, generation = registry.lease_live_turn_with_generation(
        owner, "known-commit-control", expected_owner_generation=generation,
    )
    endpoint = urlsplit(json.loads((native.config / "models.json").read_text())[
        "providers"]["response-local"]["baseUrl"])
    probe = native.root / "native-compaction-hooks.jsonl"
    release = native.root / "release-compaction-hook"
    command = "sdk-known-compaction-probe"
    install_event_host(monkeypatch, command, f"{endpoint.scheme}://{endpoint.netloc}",
        compaction_probe=probe,
        compaction_release=release if continuation == "cancel_hook" else None,
        native_settings={"compaction": {
            "enabled": False, "reserveTokens": 2048, "keepRecentTokens": 1},
            "retry": {"enabled": False}})
    arguments = ["--provider", "response-local", "--model", "fixture", "--thinking", "off",
        "--offline", "--no-extensions", "--no-skills", "--no-context-files",
        "--no-prompt-templates", "--no-tools"]
    await NativeSessionPreparation.open(native.persistent, command, arguments,
        worktree=str(native.project), environment=dict(os.environ),
        session_file=created.session_file)
    acquired = native.persistent.custody.idle()
    launch = acquired.child.key[0]

    async def prepare(text):
        return await prepare_selected_native_source(native.persistent,
            session_file=created.session_file, expected_package=launch.package,
            selected=SelectedModel("response-local", "fixture", 2000000),
            settings=PiCompactionSettings(2048, 1), retained_text=text)

    async with OwnerCompactionCommit.open(native.root / "registry.json", launch.package,
            created.session_file, native_launch=launch) as bridge:
        prepared, source = await bridge.prepare_source(owner, generation,
            prepared=(await prepare("")).require_ready(), prepare=prepare)
        async def commit():
            return await _commit_native_summary(bridge, owner, generation, native.persistent,
                prepared, source, NativeSummary(source.retained.text + "\n\nControlled SDK commit.", None, None),
                reason=ManualCompactionReason)

        if continuation == "receipt_mismatch":
            exchange = AgentCommsRestoreCompaction.exchange

            async def altered(request, *args, **kwargs):
                # Alter only this external request's claimed receipt. The real
                # native reconciliation must reject it; journal evidence stays.
                return await exchange(replace(request, expected=replace(
                    request.expected, metadata_digest="0" * 64)), *args, **kwargs)

            monkeypatch.setattr(AgentCommsRestoreCompaction, "exchange", altered)
            with pytest.raises(ValueError):
                await commit()
        elif continuation == "cancel_hook":
            committing = asyncio.create_task(commit())
            try:
                async with asyncio.timeout(20):
                    while not (probe.exists() and '"session_compact"' in probe.read_text()):
                        await asyncio.sleep(0.01)
                committing.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await committing
            finally:
                release.touch()
                if not committing.done():
                    committing.cancel()
                await asyncio.gather(committing, return_exceptions=True)
        else:
            operation = await commit()

        if continuation != "next_input":
            assert isinstance(native.persistent.custody, ReopenNative)
            assert not acquired.child.proc.alive() and acquired.child.proc.retired
            assert native.provider.posts == 0 and native.starts == []
            with bridge.journal.transaction() as db:
                rows = tuple(CompactionOperation.for_session(db, created.session_file))
            assert len(rows) == 1 and rows[0].state.declared_name == "committed"
            assert hashlib.sha256(donor.read_bytes()).hexdigest() == original
            print("known_compaction_refusal", continuation, rows[0].commit_id,
                  acquired.child.proc.pid, acquired.child.proc.retired, flush=True)
            return
    assert native.persistent.custody.idle().child is acquired.child
    hooks = [json.loads(line) for line in probe.read_text().splitlines()]
    assert [entry["event"] for entry in hooks] == ["session_start", "session_compact"]
    assert hooks[-1]["entryId"] == operation.committed_outcome().entry_id
    assert hooks[-1]["originalSessionId"] == created.session_id
    assert native.provider.posts == 0
    # Consume the original acquired SDK launch. The observation host is not a
    # second installed executable; resolving its label as a launcher would test
    # an unsupported command rather than this held-resource continuation.
    try:
        async with asyncio.timeout(30), native.persistent.lock:
            async with session_writer_fence(created.session_file):
                async with aclosing(backend.TurnSession(launch,
                        "Distinct input after the known commit",
                        persistent_session=native.persistent, native_start=native.started).run()) as stream:
                    result = [event async for event in stream]
    finally:
        await backend.terminate_task_process(asyncio.current_task())
    assert result[-1].ok, result[-1]
    assert native.persistent.custody.idle().child is acquired.child
    assert native.provider.posts == 1
    assert len(native.starts) == 1
    assert native.starts[0][2] == "Distinct input after the known commit"
    assert sum(message.get("inputId") == native.starts[0][1]
        for message in native.saved_inputs()) == 1
    assert hashlib.sha256(donor.read_bytes()).hexdigest() == original
    await native.persistent.close_idle()
    assert not acquired.child.proc.alive() and acquired.child.proc.retired
    assert await acquired.child.stderr_task == ""
    print("known_compaction_runtime", json.dumps({"donor_sha256": original,
        "source_bytes": donor.stat().st_size, "fork": created.session_file,
        "pid": acquired.child.proc.pid, "hooks": hooks, "commit": operation.commit_id,
        "next_input_terminal": result[-1].ok, "provider_requests": native.provider.posts,
        "child_retired": acquired.child.proc.retired}), flush=True)
