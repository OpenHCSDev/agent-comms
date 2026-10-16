"""Installed Core, real Pi SDK, saved ACP history and actual project-sync IPC.

Run with the installed Python, an isolated output directory and the existing
PI_COMPACTION_TEST_PACKAGE contract. The provider is localhost, never paid.
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_backend_fixture import native_backend_fixture
from agent_comms.child_process import AttachedChild
from agent_comms.native_project import NativeProject
from agent_comms.project_runtime import ProjectRuntimeRequest
from agent_comms.thread_identity import ThreadIncarnation
import agent_comms


async def run(output: Path) -> None:
    output.mkdir(mode=0o700)  # Never reuse an uncertain run's directory.
    assert Path(agent_comms.__file__).is_relative_to(sys.prefix), agent_comms.__file__
    queries, birth_argv = [], {}
    original_start, original_result = AttachedChild.start, ProjectRuntimeRequest.result

    async def observe_start(*args, **kwargs):
        child = await original_start(*args, **kwargs)
        # Node subsequently overwrites its OS argv with the title "pi".
        birth_argv[child.pid] = Path(f"/proc/{child.pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        return child

    async def observe_result(request, context):
        result = await original_result(request, context)
        queries.append({"request": request.to_wire(), "result": result})
        return result

    with patch.object(ProjectRuntimeRequest, "result", observe_result), patch.object(AttachedChild, "start", staticmethod(observe_start)):
        async with native_backend_fixture(output) as fixture:
            await fixture.author_history()
            (fixture.project / "owned.txt").write_text("Original project-sync installed acceptance.\n")
            fixture.provider.tool_call = ("read", {"path": "owned.txt"})
            package = Path(os.environ["PI_COMPACTION_TEST_PACKAGE"])
            options = ("--tools", "read", "--extension", str(package / "agent-comms-extensions/global-agent-comms/index.mjs"),
                       "--extension", str(package / "agent-comms-extensions/global-project-sync/index.mjs"))
            async with fixture.open_owner(runtime_enabled=True, native_options=options) as (owner, session):
                child = fixture.children[-1]
                argv = birth_argv[child.pid]
                (output / "child-cmdline.json").write_text(json.dumps(argv, indent=2) + "\n")
                later = Path(f"/proc/{child.pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
                (output / "attested-cmdline.json").write_text(json.dumps(later) + "\n")
                project = NativeProject.from_arguments(argv).require_managed()
                identity = ThreadIncarnation.from_arguments(argv).require_recorded()
                registered = owner._comms.registry.require(session)
                assert identity == registered.incarnation
                assert project.request.binding.owner.incarnation == identity
                assert project.socket.path.parent.exists()
                assert fixture.provider.posts == 0
                async with asyncio.timeout(35):
                    response = await owner.prompt(session, [{"type": "text", "text": "Read owned.txt once, then finish. Authored local acceptance input."}])
                assert response.stop_reason == "end_turn", response
                assert fixture.provider.posts == 2
                assert queries, "Actual pre-tool project-sync event did not call original runtime IPC"
                assert all(q["result"]["worktree"] == str(fixture.project) for q in queries)
                assert project.socket.path.parent.exists(), "Socket address lost while child was retained"
                report = {"core_module": agent_comms.__file__, "child_pid": child.pid,
                    "identity_argv": list(identity.cli_arguments), "project_argv": list(project.cli_arguments),
                    "saved_history_opened": True, "localhost_posts": fixture.provider.posts,
                    "project_queries": queries, "paid_calls": 0, "response": response.stop_reason}
    assert not project.socket.path.parent.exists(), "Original borrowed directory survived retirement"
    report.update(children_retired=True, socket_directory_retired=True)
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output.resolve()))
