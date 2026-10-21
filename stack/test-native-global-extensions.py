#!/usr/bin/env python3
"""Actual automatic extension discovery and saved-session RPC, no provider input.

Discover the manifest's compiled global snapshots in an owned agent directory.
The native copy and user globals stay read-only. Linux seccomp denies network.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import time
import tempfile

from agent_comms.candidate_maintenance import drain_candidate_catchup
from agent_comms.field_codec import FieldCodec
from agent_comms.storage_rules import StorageRoots
from agent_comms.native_package import NativeImportsManifest, VerifiedNativePackage
from agent_comms.launch_runtime import RestartEnvironment
from agent_comms.native_arguments import NativeArguments


def main(fixture: Path):
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path, nargs="?", default=os.environ.get("PI_NATIVE_PACKAGE_DIR"))
    parser.add_argument("fixture", type=Path, nargs="?", default=fixture)
    parser.add_argument("--session", type=Path)
    args = parser.parse_args()
    package, root = args.package.resolve(), args.fixture.resolve()
    VerifiedNativePackage.acquire(package)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    agent, project, commands = (root / name for name in ("agent", "project", "bin"))
    for directory in (agent, project, commands):
        directory.mkdir(mode=0o700)
    # Automatic discovery, not -e or a settings roster. The manifest owns which
    # entries have global source snapshots; no live source is installed or read.
    manifest = FieldCodec.decode(NativeImportsManifest, FieldCodec.parse(
        (package / "dist/agent-comms-imports.json").read_text()))
    globals_ = [package / entry.entry for entry in manifest.extension_entries if entry.sources]
    extensions = agent / "extensions"
    extensions.mkdir(mode=0o700)
    for entry in globals_:
        (extensions / f"{entry.parent.name}.js").symlink_to(entry)
    (agent / "settings.json").write_text('{"packages":[]}')
    repo = Path(__file__).resolve().parent.parent
    tool_cli = commands / "agent-comms"
    tool_cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agent_comms.cli "$@"\n')
    tool_cli.chmod(0o700)
    environment = {
        "PATH": f"{commands}:{os.environ['PATH']}", "HOME": str(root),
        "PYTHONPATH": str(repo / "src"), "AGENT_COMMS_ROOT": str(root / "wire"),
        "PI_OFFLINE": "1", "NODE_DISABLE_COMPILE_CACHE": "1",
    }
    environment.update(RestartEnvironment(native_config=agent, agent_directory=agent).encode_native())
    from agent_comms.child_process import ProcessIdentity
    from agent_comms.comms import Comms
    from agent_comms.threads import Thread

    comms = Comms(root / "wire")
    thread = Thread("native-startup-fixture", frozenset(), str(project),
                    storage_roots=StorageRoots(root / "scratch"),
                    process_identity=ProcessIdentity.capture(os.getpid()))
    thread = comms.registry.declare(thread)
    environment.update(thread.native_environment(comms.root, comms.registry.snapshot(), str(project)))
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
    assert len(registered["extensions"]) == len(globals_), registered
    tool_names = {tool for extension in registered["extensions"] for tool in extension["tools"]}
    assert {"subagent", "web_search"} <= tool_names, tool_names
    assert any("session_start" in item["handlers"] for item in registered["extensions"]), registered
    # The managed project guard registers its handler only with an owner name;
    # registering does not execute it or mutate the live owner.
    assert any("tool_call" in item["handlers"] for item in registered["extensions"]), registered
    session = root / "session.jsonl"
    if args.session:
        for suffix in ("", ".input-proof"):
            source = Path(str(args.session) + suffix)
            # A native fork has historical messages but no child's committed
            # context journal until its first original input. Copy an existing
            # journal exactly; absence never grants input or replay authority.
            if suffix == ".input-proof" and not source.exists():
                continue
            subprocess.run(["cp", "--reflink=auto", str(source), str(session) + suffix], check=True)
            Path(str(session) + suffix).chmod(0o600)
    else:
        session.write_text(json.dumps({"type":"session", "version":3,
            "id":"00000000-0000-4000-8000-000000000001", "timestamp":"2026-09-28T00:00:00.000Z",
            "cwd":str(project)}) + "\n")
        session.chmod(0o600)
    saved_source_sha256 = hashlib.sha256(session.read_bytes()).hexdigest()
    settings_sha256 = hashlib.sha256((agent / "settings.json").read_bytes()).hexdigest()
    started = time.monotonic()
    arguments = thread.native_launch_arguments(NativeArguments.parse(
        ("--offline", "--mode", "rpc", "--session-dir", str(root), "--session", str(session))),
        package, comms.root)
    child = subprocess.Popen(prefix + ["--import", str(package / "dist/agent-comms-project-bootstrap.mjs"),
        str(package / "dist/cli.js"), *arguments],
        cwd=project, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, preexec_fn=deny)
    reply = None
    extension_errors = []
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
            if message.get("type") == "extension_error":
                extension_errors.append(message)
            if message.get("id") == "startup":
                reply = message
                break
        assert reply and reply.get("success"), f"No successful get_state; exit={child.poll()}"
        assert not extension_errors, extension_errors
        assert reply["data"]["nativeInputProofCapability"] == "pi-native-input-v1-live-only", reply
        state_received_seconds = time.monotonic() - started
    finally:
        child.terminate()
        _, stderr = child.communicate(timeout=10)
        (root / "startup-stderr.txt").write_text(stderr)
    assert "Failed to load extension" not in stderr, stderr
    model = reply["data"].get("model")
    print(json.dumps({"ok":True, "automatic_extensions":registered, "get_state":{
        "success":reply["success"], "messageCount":reply["data"]["messageCount"],
        "nativeInputProofCapability":reply["data"]["nativeInputProofCapability"],
        "model_field_present":"model" in reply["data"],
        "model":{key:model[key] for key in ("provider", "id")} if model else None,
        "thinkingLevel":reply["data"]["thinkingLevel"]},
        "saved_session_bytes":session.stat().st_size, "network":"kernel-denied",
        "saved_source_sha256_before":saved_source_sha256,
        "saved_source_sha256_after":hashlib.sha256(session.read_bytes()).hexdigest(),
        "settings_sha256_before":settings_sha256,
        "settings_sha256_after":hashlib.sha256((agent / "settings.json").read_bytes()).hexdigest(),
        "state_received_seconds":state_received_seconds, "native_child_retired":child.poll() is not None,
        "provider_prompts":0}, indent=2))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="native-globals-") as directory:
        try:
            main(Path(directory) / "fixture")
        finally:
            drain_candidate_catchup(Path(directory))
