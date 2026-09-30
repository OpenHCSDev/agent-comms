"""Authorized real Sol/high retained forks; private installed native/ACP/UI journey.

No provider substitutions, public inputs, live-owner restarts or uncertain retries.
Launch credentials/settings remain in RAM. Only aggregate observations are exported.
"""
import asyncio
import json
import os
import re
import shlex
import sys
import time
import traceback
from importlib.resources import files
from pathlib import Path

from agent_comms.comms import Comms
from agent_comms.compaction_journal import CompactionJournal
from agent_comms.compaction_states import LinkedSummary, ManualCommittedSummary
from agent_comms.input_disposition import InputDispositions
from agent_comms.input_attempt import NotSentInput
from agent_comms.native_fork import ForkSessionHelper, ForkSessionRequest
from agent_comms.native_package import verify_native_package
from agent_comms.owner_launch import RetainedOwnerLaunch
from agent_comms.registration import Registration
from agent_comms.threads import Thread
from agent_comms.turn_phase import CompactionPhase
from toad.agent_schema import AgentDefinition
from toad.app import ToadApp
from toad.widgets.agent_response import AgentResponse
from toad.widgets.transcript_history import TranscriptHistory

from l0a_native_installed_pilot import until
from runtime_fixture import stop_test_children
from saved_state_user_journey_pilot import submit_editor


class CompactionApp(ToadApp):
    CSS_PATH = files("toad").joinpath("toad.tcss")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.compaction_frames = []

    def _display(self, *args, **kwargs):
        result = super()._display(*args, **kwargs)
        frame = "\n".join(strip.text for strip in self.screen._compositor.render_strips())
        observation = {
            "draft": "Compaction summary · draft" in frame,
            "committed": "Context compacted" in frame,
            "elapsed_visible": bool(re.search(r"\d+:\d{2} elapsed", frame)),
            "source_visible": "% of input processed" in frame,
        }
        if any(observation.values()):
            self.compaction_frames.append({"time": time.monotonic(), **observation})
        return result


async def main():
    assert os.environ["AC_REAL_PROVIDER_AUTHORIZED"] == "Sol/high retained acceptance"
    stage = Path(os.environ["AC_REAL_FIXTURE_STAGE"])
    evidence = Path(os.environ["L0A_EVIDENCE"])
    assert stage.is_relative_to("/home/ts/wt")
    stage.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=True)
    source_root = Path(os.environ["AC_REAL_SOURCE_ROOT"])
    source_snapshot = Registration(source_root / "registry.json").snapshot()
    source = source_snapshot.require_active("nra-architecture")
    retained = RetainedOwnerLaunch.capture(source, source_snapshot)
    assert source.model and "sol" in source.model.lower()
    assert source.thinking_level.declared_name == "high"
    source_file = Path(os.environ["AC_REAL_SOURCE_FILE"])
    assert source_file.stat().st_size >= 40_000_000
    package = Path(os.environ["AC_NATIVE_COPIED_PACKAGE"])
    verify_native_package(package)
    project = stage / "project"
    project.mkdir()
    environment = dict(retained.environment)
    fork_env = dict(environment, PI_CODING_AGENT_DIR=str(stage / "native-forks"))
    forks = [await ForkSessionHelper.run(
        ForkSessionRequest(str(package), str(source_file), str(project)),
        cwd=project, env=fork_env,
    ) for _ in range(2)]
    assert all(Path(identity.session_file).is_relative_to(stage) for identity in forks)
    service = Comms(stage / "wire")
    root_id = service.messaging.initialize_private_initial_protocol()
    service.owners.pin_private_nk_launch(service.root, root_id, package)
    runtime_path = str(Path(sys.executable).parent)
    environment.update(
        AGENT_COMMS_ROOT=str(service.root),
        AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
        AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=str(package),
        AGENT_COMMS_AGENT_BIN=str(Path(sys.executable).with_name("pi-comms-native")),
        AGENT_COMMS_AGENT_ARGS=shlex.join(retained.arguments or ()),
        PATH=runtime_path + os.pathsep + environment.get("PATH", ""),
        VIRTUAL_ENV=str(Path(sys.executable).parent.parent),
        AGENT_COMMS_RUNTIME_ROOT=runtime_path,
        AGENT_COMMS_DEBUG_LOG=str(stage / "acp-debug"),
        XDG_CONFIG_HOME=str(stage / "config"),
        XDG_STATE_HOME=str(stage / "state"),
        XDG_DATA_HOME=str(stage / "data"),
        TOAD_TEST_ATTEMPT=stage.name,
    )
    for name in ("PI_PROMPT", "PI_PARENT_ID", "PI_TASK", "PI_AGENT_ID",
                 "AGENT_COMMS_THREAD", "AGENT_COMMS_STARTUP_INPUT_KEY", "PYTHONPATH"):
        environment.pop(name, None)
    os.environ.clear()
    os.environ.update(environment)
    for mode, identity in zip(("automatic", "manual"), forks, strict=True):
        service.registry.declare(Thread(
            mode, frozenset({"compaction-acceptance"}), str(project),
            session_file=identity.session_file, model=source.model,
            thinking_level=source.thinking_level,
            task="Bounded acceptance only. Do not resume inherited work, goals or tools. "
                 "Answer only the explicitly requested acceptance token.",
        ))
    definition = AgentDefinition.decode({
        "name": "Real compaction acceptance", "identity": "real-compaction", "short_name": "real",
        "protocol": "acp", "run_command": {"*": shlex.join([sys.executable, "-m", "agent_comms.acp"])},
    })
    receipt = {"provider": source.model, "thinking": source.thinking_level.declared_name,
               "source_file": str(source_file), "source_bytes": source_file.stat().st_size,
               "prepared_native": str(package), "attempt": stage.name,
               "modes": [], "complete": False}
    journal = CompactionJournal(service.root / "compaction-commits.sqlite3")
    print("REAL_COMPACTION_STAGE", json.dumps({"pid": os.getpid(), "stage": str(stage),
          "root": str(service.root), "source_bytes": receipt["source_bytes"]}), flush=True)
    try:
        for mode, identity in zip(("automatic", "manual"), forks, strict=True):
            app = CompactionApp(agent_data=definition, project_dir=str(project), agent_session_id=mode)
            observations = []
            case = {"mode": mode, "native_fork": identity.session_file,
                    "observations": observations, "complete": False}
            receipt["modes"].append(case)
            async with app.run_test(size=(160, 44)) as pilot:
                view = app.selected_session.conversation
                await until(pilot, lambda: view.agent is not None and view.agent_ready, 90)
                assert view.agent.session.connected
                await until(pilot, lambda: bool(view.contents.query(TranscriptHistory)), 90)
                if app._exception is not None:
                    raise app._exception
                token = "REAL_COMPACTION_ONCE_ONLY_437"
                command = (
                    "Bounded acceptance only. Do not resume inherited work or use tools. "
                    f"Reply exactly {token}."
                    if mode == "automatic" else
                    "/compact Preserve current decisions, exact paths and unfinished tasks concisely."
                )
                began = time.monotonic()
                await submit_editor(pilot, view.prompt.prompt_text_area, command)
                print("REAL_COMPACTION_SUBMITTED", mode, flush=True)
                saw_source = False
                async with asyncio.timeout(900):
                    while True:
                        thread = service.registry.require(mode)
                        phase = thread.turn_state.phase
                        if isinstance(phase, CompactionPhase) and phase.source is not None:
                            value = phase.source
                            observation = {"wall_seconds": round(time.monotonic() - began, 3),
                                "operation": phase.operation_id, "source_done": value.source_bytes_done,
                                "source_total": value.source_bytes_total, "phase": value.summary_phase,
                                "started_at_ms": value.started_at_ms, "observed_at_ms": value.observed_at_ms,
                                "native_elapsed_ms": value.elapsed_ms}
                            if not observations or observation["observed_at_ms"] != observations[-1]["observed_at_ms"]:
                                observations.append(observation)
                            saw_source = True
                        summaries = journal.summaries.history(thread.session_file)
                        if summaries and isinstance(summaries[-1].state, LinkedSummary):
                            if mode == "manual" or (
                                thread.last_finished_turn_id and not thread.executing and
                                any(token in body.source for body in view.contents.query(AgentResponse))
                            ):
                                break
                        if summaries and summaries[-1].state.terminal and not isinstance(summaries[-1].state, LinkedSummary):
                            raise AssertionError(f"Real compaction {summaries[-1].state.declared_name}; original preserved, no retry")
                        inputs = [row for row in InputDispositions(service.root / InputDispositions.filename).read().rows.values() if row.owner == mode]
                        if any(row.unresolved and not row.accepts_reservation for row in inputs) and not thread.executing:
                            raise AssertionError("Real input uncertain; original preserved, no retry")
                        if any(isinstance(row, NotSentInput) for row in inputs) and not thread.executing:
                            raise AssertionError("Real input not sent; inspect the canonical failure, no retry")
                        if app._exception is not None:
                            raise app._exception
                        await pilot.pause(0.2)
                assert saw_source, "Original large context did not exercise automatic/manual compaction"
                assert any(f["draft"] for f in app.compaction_frames), "Summary was not painted before commit"
                assert any(f["source_visible"] and f["elapsed_visible"] for f in app.compaction_frames)
                assert len({s["operation"] for s in observations}) == 1
                assert len({s["started_at_ms"] for s in observations}) == 1
                assert all(s["observed_at_ms"] >= s["started_at_ms"] for s in observations)
                committed = journal.operations.get(summaries[-1].state.commit_id)
                committed.state.require_committed(committed.commit_id)
                case["native_commit_id"] = committed.commit_id
                case["canonical_session"] = thread.session_file
                inputs = [row for row in InputDispositions(service.root / InputDispositions.filename).read().rows.values() if row.owner == mode]
                original_inputs = [row for row in inputs if row.digest.matches(command)]
                case["original_input_starts"] = sum(row.has_started for row in original_inputs)
                assert case["original_input_starts"] == (1 if mode == "automatic" else 0)
                case.update(elapsed_seconds=round(time.monotonic() - began, 3),
                            frames=app.compaction_frames, complete=True)
                if mode == "manual":
                    assert isinstance(summaries[-1].state, ManualCommittedSummary)
                app.save_screenshot(str(evidence / f"{mode}-completed.svg"))
                print("REAL_COMPACTION_COMPLETE", mode, case["elapsed_seconds"], flush=True)
        receipt["complete"] = True
    except BaseException:
        (evidence / "failure.txt").write_text(traceback.format_exc())
        raise
    finally:
        (evidence / "receipt.json").write_text(json.dumps(receipt, indent=2))
        for thread in service.registry.all_threads().values():
            if thread.role.executable and thread.process_alive:
                await asyncio.to_thread(service.owners.stop, thread.name)
        await stop_test_children(stage.name)
        assert all(not thread.process_alive for thread in service.registry.all_threads().values())
        (evidence / "cleanup.json").write_text(json.dumps({"remaining_owned_children": []}))
    print("REAL_COMPACTION_ACCEPTANCE", receipt["complete"], flush=True)


if __name__ == "__main__":
    asyncio.run(main())
