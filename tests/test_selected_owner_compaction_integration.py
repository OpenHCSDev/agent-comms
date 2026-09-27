"""Normal prepared bundle: selected RPC -> native commit -> one original bind."""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agent_comms import agent_events as ae
from agent_comms.backend import PersistentPiSession, _JsonLineReader, _session_revision
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_send_admission import native_input_admitted
from agent_comms.declarations import AgentRuntimeInfo, Goal, Thread, ThreadRegistry, _store_lock
from agent_comms.input_disposition import InputDispositions
from agent_comms.owner_compaction_adaptive import maybe_compact_owner_turn
from agent_comms.owner_compaction_settings import PiCompactionDecision

PACKAGE = os.environ.get("PI_COMPACTION_TEST_PACKAGE")
pytestmark = pytest.mark.skipif(not PACKAGE, reason="Normal prepared native bundle required")


@asynccontextmanager
async def owner_fixture(tmp_path, monkeypatch, *, real_host=False):
    package = Path(PACKAGE)
    launcher = str(package.parents[3] / "bin/pi-native")
    repo = Path(__file__).resolve().parents[1]
    child = await asyncio.create_subprocess_exec(
        "node",
        str(
            repo
            / (
                "stack/test-native-selected-owner-host.mjs"
                if real_host
                else "stack/test-native-selected-compaction-summary.mjs"
            )
        ),
        env=dict(
            os.environ,
            PI_NATIVE_PACKAGE_DIR=PACKAGE,
            PR95_OWNER_FIXTURE_ROOT=str(tmp_path),
            PR95_RPC_FIXTURE="1",
            PR95_KEEP_SOURCE="1",
            TMPDIR=str(tmp_path),
        ),
        start_new_session=True,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    persistent = PersistentPiSession()
    persistent.proc = child
    try:
        async with asyncio.timeout(5):
            line = await child.stderr.readline()
        assert line.startswith(b"{"), line.decode()
        fixture = json.loads(line)
        file = fixture["sessionFile"]
        persistent.reader = _JsonLineReader(child.stdout)
        persistent.session_file = file
        persistent.session_id = (
            fixture["sessionId"] if real_host else fixture["witness"]["sessionId"]
        )
        persistent.revision = _session_revision(file)
        persistent.launch_key = (launcher,)
        registry = ThreadRegistry(tmp_path / "registry.json")
        registry.register(
            Thread(
                "owner",
                frozenset(),
                str(tmp_path),
                pid=os.getpid(),
                session_file=file,
                model=fixture["model"] if real_host else "fake/fake",
                goal=Goal("work", "goal"),
            )
        )
        owner, epoch = registry.live_owner_with_epoch("owner")
        owner, epoch = registry.claim_live_turn_with_epoch(owner, "turn", expected_epoch=epoch)
        inputs = InputDispositions(tmp_path)
        inputs.record(
            "acp:original",
            seq=None,
            owner="owner",
            admission=owner.active_turn.admission_generation,
            target="owner",
            text="Continue",
        )
        agent_dir = tmp_path / "pi-settings"
        agent_dir.mkdir()
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
        monkeypatch.setattr(
            "agent_comms.owner_compaction_adaptive.read_compaction_decision",
            lambda *a, **kw: PiCompactionDecision(True, 1000, 10, True),
        )
        info = AgentRuntimeInfo(
            thread="owner",
            model=fixture["model"] if real_host else "fake/fake",
            context_used=9500,
            context_size=fixture["contextWindow"] if real_host else 10000,
        )
        yield persistent, registry, inputs, file, launcher, info
    finally:
        await persistent.close_idle()


async def test_selected_native_summary_commits_and_admits_original_exactly_once(
    tmp_path, monkeypatch
):
    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        admitted = []
        assert (
            await maybe_compact_owner_turn(
                registry,
                launcher,
                "owner",
                "turn",
                info,
                "acp:original",
                persistent,
                input_text="Continue",
                on_admission=admitted.append,
            )
            is True
        )
        assert len(admitted) == 1
        journal = CompactionJournal(tmp_path / "compaction-commits.sqlite3")
        rows = journal.blocking_selected_summary(file)
        assert len(rows) == 1 and rows[0].status == "linked"
        operation = journal.get(rows[0].commit_id)
        assert operation.status == "committed"
        intent = json.loads(operation.intent_json)
        assert intent["selectedSummaryOperationId"] == rows[0].operation_id
        assert (
            intent["selectedSummarySourceDigest"]
            == hashlib.sha256(rows[0].source_json.encode()).hexdigest()
        )
        entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in entries) == 1
        assert len(journal.pending_publications(file)) == 1
        assert persistent.proc is None and persistent.reopen_required == file
        assert not native_input_admitted(tmp_path, file)
        token = admitted[0]
        assert inputs.get("acp:original")["native_id"] is None
        with _store_lock(tmp_path / "wire"):
            assert token.consume_bound_original(
                wire_root=tmp_path,
                session_file=file,
                identity=token._identity,
                native_id="a" * 32,
                sent_text="Continue",
                dispositions=inputs,
            )
            assert not token.consume_bound_original(
                wire_root=tmp_path,
                session_file=file,
                identity=token._identity,
                native_id="b" * 32,
                sent_text="Continue",
                dispositions=inputs,
            )
        assert inputs.get("acp:original")["native_id"] == "a" * 32


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("real_host", [False, True])
async def test_acp_selected_summary_handoff_uses_final_prompt_once(
    tmp_path, monkeypatch, correction, real_host
):
    from dataclasses import replace

    from agent_comms import backend
    from agent_comms.acp import CommsAgent
    from agent_comms.declarations import RelationViolationError
    from agent_comms.goal_attempts import GoalAttemptStore
    from agent_comms.operations import wire
    from agent_comms.selected_pi_summary_rpc import SelectedSummarySlot

    async with owner_fixture(tmp_path, monkeypatch, real_host=real_host) as (
        persistent,
        _registry,
        _inputs,
        file,
        launcher,
        info,
    ):
        root = tmp_path / "acp-wire"
        comms = wire(root)
        project = tmp_path / "proj"
        project.mkdir()

        class Client:
            async def session_update(self, **kwargs):
                pass

        agent = CommsAgent(
            comms,
            agent_bin=launcher,
            agent_args=[],
            runtime_enabled=True,
            adaptive_compaction_enabled=True,
            auto_wake=False,
        )
        agent.on_connect(Client())
        await agent.new_session(cwd=str(project), mcp_servers=[])
        agent._drain_tasks["proj"].cancel()
        await asyncio.gather(agent._drain_tasks["proj"], return_exceptions=True)
        comms.registry.register(
            replace(
                comms.registry.require("proj"),
                goal=Goal("retain history", "goal-acp"),
                session_file=file,
                model=info.model,
                pid=os.getpid(),
            )
        )
        comms.set_agent_info(
            "proj",
            model=info.model,
            context_used=info.context_used,
            context_size=info.context_size,
        )
        private = root / "goal-private"
        private.mkdir(mode=0o700)
        store = GoalAttemptStore.initialize(private)
        store.create_goal("goal-acp")
        agent._goal_store = store
        agent._persistent_backends["proj"] = persistent
        dispositions = InputDispositions(root)
        dispositions.record(
            "acp:original",
            seq=None,
            owner="proj",
            admission=comms.registry.snapshot().admission_generations["proj"],
            target="proj",
            text="Continue",
        )
        selected_exchange = SelectedSummarySlot.run_selected_summary

        async def selected_with_correction(self, *args, **kwargs):
            result = await selected_exchange(self, *args, **kwargs)
            if correction:
                dispositions.record(
                    "acp:correction",
                    seq=None,
                    owner="proj",
                    admission=comms.registry.snapshot().admission_generations["proj"],
                    target="proj",
                    text="Changed requirement before native commit",
                )
            return result

        monkeypatch.setattr(SelectedSummarySlot, "run_selected_summary", selected_with_correction)
        dispatched = []

        async def native_stream(*args, **kwargs):
            task = args[2]
            # Exercise actual ACP admission/bind callbacks; no provider or
            # second native user turn is started by this synthetic receiver.
            assert task != "Continue" and task.endswith("Continue")
            assert persistent.proc is None and persistent.reopen_required == file
            with kwargs["send_boundary"](None, "a" * 32, task) as allowed:
                assert allowed is True
                dispatched.append(task)
            with kwargs["send_boundary"](None, "b" * 32, task) as allowed:
                assert allowed is False
            assert kwargs["native_start"](None, "a" * 32, task)
            yield ae.InputStarted(id=None)
            yield ae.StreamSettled()
            yield ae.Done(ok=True, text="processed")

        if real_host:
            # Use the real backend subprocess/reopen/proof protocol. Replace
            # only its executable with an offline host of the same prepared
            # SDK and RPC; the selected model stream never accesses a network.
            spawn = asyncio.create_subprocess_exec

            async def offline_spawn(program, *args, **kwargs):
                if program == launcher:
                    kwargs["env"]["PR95_OWNER_FIXTURE_ROOT"] = str(tmp_path)
                    return await spawn(
                        "node",
                        str(
                            Path(__file__).resolve().parents[1]
                            / "stack/test-native-selected-owner-host.mjs"
                        ),
                        *args,
                        **kwargs,
                    )
                return await spawn(program, *args, **kwargs)

            monkeypatch.setattr(asyncio, "create_subprocess_exec", offline_spawn)
        else:
            monkeypatch.setattr(backend, "stream_agent_events", native_stream)
        try:
            turn = agent._run_agent_turn(
                "proj",
                "proj",
                "Continue",
                original_keys=("acp:original",),
                original_owner_input=True,
                original_goal_id="goal-acp",
            )
            if correction:
                with pytest.raises(RelationViolationError, match="Unsettled"):
                    await turn
                assert dispatched == []
                assert dispositions.get("acp:original")["native_id"] is None
            else:
                await turn
                assert len(dispatched) == (0 if real_host else 1)
                assert dispositions.status("acp:original") == "started"
            journal = CompactionJournal(root / "compaction-commits.sqlite3")
            rows = journal.blocking_selected_summary(file)
            assert len(rows) == 1
            assert rows[0].status == ("reserved" if correction else "linked")
            entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
            assert sum(row["type"] == "compaction" for row in entries) == (0 if correction else 1)
            assert "proj" not in agent._selected_summary_admissions
            if real_host and not correction:
                assert persistent.reopen_required is None
                assert persistent.proc is not None
                native_id = dispositions.get("acp:original")["native_id"]
                assert native_id
                user_entries = [
                    row
                    for row in entries
                    if row["type"] == "message"
                    and row["message"]["role"] == "user"
                    and row["message"].get("inputId") == native_id
                ]
                assert len(user_entries) == 1
        finally:
            await agent.shutdown()


async def test_correction_after_native_commit_never_mints_original_admission(tmp_path, monkeypatch):
    from agent_comms.declarations import RelationViolationError
    from agent_comms.owner_compaction_commit import OwnerCompactionCommit

    async with owner_fixture(tmp_path, monkeypatch) as (
        persistent,
        registry,
        inputs,
        file,
        launcher,
        info,
    ):
        admit = OwnerCompactionCommit.admit_selected_original

        def corrected(self, owner, epoch, operation, source, identity):
            assert operation.status == "committed"
            inputs.record(
                "acp:correction",
                seq=None,
                owner=owner.name,
                admission=owner.active_turn.admission_generation,
                target=owner.name,
                text="Correction after native commit",
            )
            return admit(self, owner, epoch, operation, source, identity)

        monkeypatch.setattr(OwnerCompactionCommit, "admit_selected_original", corrected)
        admissions = []
        with pytest.raises(RelationViolationError, match="Unsettled"):
            await maybe_compact_owner_turn(
                registry,
                launcher,
                "owner",
                "turn",
                info,
                "acp:original",
                persistent,
                input_text="Continue",
                on_admission=admissions.append,
            )
        assert admissions == []
        assert inputs.get("acp:original")["native_id"] is None
        entries = [json.loads(line) for line in Path(file).read_text().splitlines()]
        assert sum(row["type"] == "compaction" for row in entries) == 1
        assert not native_input_admitted(tmp_path, file)
