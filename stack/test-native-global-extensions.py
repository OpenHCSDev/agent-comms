#!/usr/bin/env python3
"""Actual automatic extension discovery and saved-session RPC, no provider input.

Use an owned persistent fixture directory. Source globals are read-only; the
native copy is never modified here. Linux seccomp denies all network syscalls.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--session", type=Path)
    args = parser.parse_args()
    package, root = args.package.resolve(), args.fixture.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    agent, project, commands = (root / name for name in ("agent", "project", "bin"))
    for directory in (agent, project, commands):
        directory.mkdir(mode=0o700)
    # Discovery sees the original global filenames; no settings-based -e list
    # and no --no-extensions flag can hide the user's normal startup path.
    (agent / "extensions").symlink_to(Path.home() / ".pi/agent/extensions", target_is_directory=True)
    (agent / "settings.json").write_text('{"packages":[]}')
    repo = Path(__file__).resolve().parent.parent
    tool_cli = commands / "agent-comms"
    tool_cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agent_comms.cli "$@"\n')
    tool_cli.chmod(0o700)
    environment = dict(os.environ, PI_CODING_AGENT_DIR=str(agent), PI_WORKTREE=str(project),
                       AGENT_COMMS_MANAGED="1", AGENT_COMMS_ROOT=str(root / "wire"),
                       PYTHONPATH=str(repo / "src"), PI_OFFLINE="1",
                       NODE_DISABLE_COMPILE_CACHE="1", PATH=f"{commands}:{os.environ['PATH']}")
    for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_COMPILE_CACHE", "PI_PARENT_ID", "PI_AGENT_ID",
                 "AGENT_COMMS_THREAD"):
        environment.pop(name, None)
    environment["AGENT_COMMS_THREAD"] = "native-startup-fixture"
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.runtime_requests import ProjectRuntimeRequest
    from agent_comms.threads import Thread

    comms = Comms(root / "wire")
    thread = Thread("native-startup-fixture", frozenset(), str(project),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    comms.registry.declare(thread)
    environment.update(ProjectRuntimeRequest.for_native(comms.registry.snapshot(), thread)
                       .environment(comms.root))
    spec = importlib.util.spec_from_file_location("native_rpc_fixture", repo / "stack/test-native-import-rpc.py")
    isolation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolation)
    deny = isolation.network_denial()
    node = shutil.which("node")
    prefix = [node, "--no-global-search-paths", "--import",
              str(package / "dist/agent-comms-import-fence.mjs")]
    probe = f"""
import {{DefaultResourceLoader}} from {json.dumps((package / 'dist/index.js').as_uri())};
const loader = new DefaultResourceLoader({{cwd:process.env.PI_WORKTREE,
    agentDir:process.env.PI_CODING_AGENT_DIR}});
await loader.reload();
const {{extensions, errors}} = loader.getExtensions();
console.log(JSON.stringify({{errors, extensions:extensions.map(e => ({{path:e.path,
    tools:[...e.tools.keys()], commands:[...e.commands.keys()], handlers:[...e.handlers.keys()]}}))}}));
"""
    registration = subprocess.run(prefix + ["--input-type=module", "-e", probe], env=environment,
                                  cwd=project, capture_output=True, text=True, timeout=30,
                                  preexec_fn=deny)
    (root / "registration-stderr.txt").write_text(registration.stderr)
    assert registration.returncode == 0, registration.stderr
    registered = json.loads(registration.stdout)
    assert not registered["errors"], registered["errors"]
    assert len(registered["extensions"]) == 4, registered
    tool_names = {tool for extension in registered["extensions"] for tool in extension["tools"]}
    assert {"subagent", "web_search", "comms_send", "comms_goal"} <= tool_names, tool_names
    # The managed project guard registers its handler only with an owner name;
    # registering does not execute it or mutate the live owner.
    project_sync = next(item for item in registered["extensions"] if '/project-sync/' in item['path'])
    assert "tool_call" in project_sync["handlers"], project_sync
    session = root / "session.jsonl"
    if args.session:
        for suffix in ("", ".input-proof"):
            source = Path(str(args.session) + suffix)
            subprocess.run(["cp", "--reflink=auto", str(source), str(session) + suffix], check=True)
            Path(str(session) + suffix).chmod(0o600)
    else:
        session.write_text(json.dumps({"type":"session", "version":3,
            "id":"00000000-0000-4000-8000-000000000001", "timestamp":"2026-09-28T00:00:00.000Z",
            "cwd":str(project)}) + "\n")
        session.chmod(0o600)
    child = subprocess.Popen(prefix + ["--import", str(package / "dist/agent-comms-project-bootstrap.mjs"),
        str(package / "dist/cli.js"), "--offline", "--mode", "rpc", "--session", str(session)],
        cwd=project, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, preexec_fn=deny)
    reply = None
    try:
        child.stdin.write('{"type":"get_state","id":"startup"}\n')
        child.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if not selector.select(timeout=max(0, deadline - time.monotonic())):
                break
            line = child.stdout.readline()
            if not line:
                break
            message = json.loads(line)
            if message.get("id") == "startup":
                reply = message
                break
        assert reply and reply.get("success"), f"No successful get_state; exit={child.poll()}"
        assert reply["data"]["nativeInputProofCapability"] == "pi-native-input-v1-live-only", reply
    finally:
        child.terminate()
        _, stderr = child.communicate(timeout=10)
        (root / "startup-stderr.txt").write_text(stderr)
    assert "Failed to load extension" not in stderr, stderr
    print(json.dumps({"ok":True, "automatic_extensions":registered, "get_state":{
        "success":reply["success"], "messageCount":reply["data"]["messageCount"],
        "nativeInputProofCapability":reply["data"]["nativeInputProofCapability"]},
        "saved_session_bytes":session.stat().st_size, "network":"kernel-denied",
        "provider_prompts":0}, indent=2))


if __name__ == "__main__":
    main()
