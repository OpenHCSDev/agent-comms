#!/usr/bin/env python3
"""Linux, provider-free canonical Pi/PR77 registration with kernel network denial."""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def network_denial() -> Callable[[], None]:
    """Filter only fixture subprocesses. Any attempted network syscall kills them."""
    library = ctypes.util.find_library("seccomp")
    if sys.platform != "linux" or not library:
        raise RuntimeError("This isolation fixture requires Linux libseccomp")
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Cannot allocate seccomp fixture")
    for name in (b"socket", b"connect", b"sendto", b"sendmsg", b"sendmmsg"):
        syscall = lib.seccomp_syscall_resolve_name(name)
        if syscall >= 0 and lib.seccomp_rule_add(context, 0x80000000, syscall, 0) != 0:
            raise RuntimeError("Cannot build seccomp network-denial fixture")

    def activate() -> None:
        if lib.seccomp_load(context) != 0:
            os._exit(125)

    return activate


def ancestor_probe(
    package: Path, root: Path, env: dict[str, str], deny: Callable[[], None]
) -> dict:
    """Admit real PR77, then reach a committed ws entry's optional ancestor require."""
    assert package.is_relative_to("/var/tmp") or package.is_relative_to(
        "/dev/shm"
    ), "Disposable package required"
    ancestor = package.parent.parent / "bufferutil"
    ancestor.mkdir(mode=0o700)  # Exclusive: never replace an existing dependency.
    marker = root / "ancestor-executed"
    code = (
        f"require('node:fs').writeFileSync({json.dumps(str(marker))}, 'executed'); "
        "module.exports={mask(){},unmask(){}};"
    )
    (root / "ancestor-sentinel.cjs").write_text(code)
    try:
        (ancestor / "package.json").write_text('{"name":"bufferutil","main":"index.cjs"}')
        (ancestor / "index.cjs").write_text(code)
        node = shutil.which("node")
        assert node
        control = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "-e",
                f"require({json.dumps(str(package / 'node_modules/ws/lib/buffer-util.js'))})",
            ],
            env=env,
            preexec_fn=deny,
            capture_output=True,
            timeout=15,
        )
        assert control.returncode == 0 and marker.exists(), (control.returncode, control.stderr)
        marker.unlink()  # Only the known disposable control marker, never session evidence.
        script = """import {registerHooks} from 'node:module';
const seen=[];
registerHooks({resolve(specifier,context,next){
  try {return next(specifier,context);} catch(error) {
    if(specifier==='bufferutil') seen.push({code:error.code,parent:context.parentURL});
    throw error;
  }
}});
const {loadApprovedExtension}=await import(process.argv[1]);
const factory=await loadApprovedExtension(process.argv[2]);
await import(process.argv[3]);
console.log(JSON.stringify({factory:typeof factory,dependencyEntry:process.argv[3],seen}));
"""
        fence = package / "dist/agent-comms-import-fence.mjs"
        entry = package / "agent-comms-extensions/pi-mcp-client/index.mjs"
        guarded = subprocess.run(
            [
                node,
                "--no-global-search-paths",
                "--import",
                str(fence),
                "--input-type=module",
                "-e",
                script,
                fence.as_uri(),
                str(entry),
                (package / "node_modules/ws/wrapper.mjs").as_uri(),
            ],
            env=env,
            preexec_fn=deny,
            capture_output=True,
            timeout=20,
        )
        assert guarded.returncode == 0, guarded.stderr
        result = json.loads(guarded.stdout)
        assert isinstance(result, dict)
        assert result["factory"] == "function" and result["seen"], result
        assert all(
            item["code"] == "ERR_NATIVE_IMPORT_BOUNDARY"
            and item["parent"].endswith("/ws/lib/buffer-util.js")
            for item in result["seen"]
        ), result
        assert not marker.exists(), "Ancestor dependency executed under fence"
        (root / "ancestor-result.json").write_text(json.dumps(result))
        return result
    finally:
        shutil.rmtree(ancestor)


def main(launcher: Path, package: Path) -> None:
    # Nothing inherited except executable lookup. No provider keys/live identity,
    # NODE_OPTIONS, user Pi configuration, project trust or saved MCP approval.
    root = Path(tempfile.mkdtemp(prefix="pr95-canonical-rpc-", dir="/var/tmp"))
    agent, project = root / "agent", root / "project"
    agent.mkdir(mode=0o700)
    project.mkdir(mode=0o700)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(root),
        "PI_CODING_AGENT_DIR": str(agent),
        "PI_OFFLINE": "1",
        "PI_SKIP_VERSION_CHECK": "1",
        "CI": "true",
        "NO_COLOR": "1",
        "TMPDIR": "/var/tmp",
    }
    deny = network_denial()
    node = shutil.which("node")
    assert node
    control = subprocess.run(
        [node, "-e", "require('net').connect(9,'127.0.0.1')"],
        env=env,
        preexec_fn=deny,
        capture_output=True,
        timeout=10,
    )
    assert control.returncode == -signal.SIGSYS, (control.returncode, control.stderr)
    extension = package / "agent-comms-extensions/pi-mcp-client"
    install = subprocess.run(
        [str(launcher), "install", str(extension)],
        cwd=project,
        env=env,
        preexec_fn=deny,
        capture_output=True,
        timeout=30,
    )
    assert install.returncode == 0, (install.returncode, install.stdout, install.stderr)
    settings = json.loads((agent / "settings.json").read_text())
    installed_paths = [
        (agent / value).resolve() for value in settings["packages"] if isinstance(value, str)
    ]
    assert extension.resolve() in installed_paths, settings
    # Package acquisition is a separate code-execution path: prove admission
    # happens before npm/git/lifecycle commands, not merely at module import.
    npm_marker = root / "unapproved-package-command"
    npm_wrapper = root / "npm-wrapper.mjs"
    npm_wrapper.write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(npm_marker))}, 'unsafe');"
    )
    guarded_settings = dict(settings, npmCommand=[node, str(npm_wrapper)])
    online_env = dict(env, PI_OFFLINE="0")  # Kernel denial, not offline skip, proves this guard.
    try:
        (agent / "settings.json").write_text(json.dumps(guarded_settings))
        for operation in ("install", "remove"):
            rejected = subprocess.run(
                [str(launcher), operation, "npm:unapproved-fixture"],
                cwd=project,
                env=online_env,
                preexec_fn=deny,
                capture_output=True,
                timeout=20,
            )
            assert (
                rejected.returncode != 0 and b"mutable npm/git package source" in rejected.stderr
            ), rejected.stderr
        guarded_settings["packages"] = [*settings["packages"], "npm:unapproved-fixture"]
        (agent / "settings.json").write_text(json.dumps(guarded_settings))
        rejected = subprocess.run(
            [str(launcher), "--mode", "rpc", "--no-session"],
            cwd=project,
            env=online_env,
            preexec_fn=deny,
            input=b"",
            capture_output=True,
            timeout=20,
        )
        assert (
            rejected.returncode != 0 and b"mutable npm/git package source" in rejected.stderr
        ), rejected.stderr
        assert not npm_marker.exists(), "Package subprocess ran before admission"
    finally:
        (agent / "settings.json").write_text(json.dumps(settings))
    ancestor = ancestor_probe(package, root, env, deny)
    marker = root / "unapproved-server-started"
    (project / "server.mjs").write_text(
        "import {writeFileSync} from 'node:fs';"
        f"writeFileSync({json.dumps(str(marker))}, 'unsafe');"
    )
    (agent / "mcp.json").write_text(
        json.dumps(
            {
                "version": 1,
                "servers": [
                    {
                        "id": "fixture",
                        "enabled": True,
                        "instructionsPolicy": "status-only",
                        "transport": {
                            "type": "stdio",
                            "command": node,
                            "args": ["./server.mjs"],
                            "cwd": "project",
                        },
                    }
                ],
            }
        )
    )
    child = subprocess.Popen(
        [
            str(launcher),
            "--offline",
            "--mode",
            "rpc",
            "--no-session",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-builtin-tools",
        ],
        cwd=project,
        env=dict(env, AGENT_COMMS_MANAGED="1", PI_WORKTREE=str(project)),
        preexec_fn=deny,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    selector = selectors.DefaultSelector()
    assert child.stdout and child.stderr and child.stdin
    selector.register(child.stdout, selectors.EVENT_READ, "stdout")
    selector.register(child.stderr, selectors.EVENT_READ, "stderr")
    pending = b""
    errors = b""
    commands = None
    status = None
    deadline = time.monotonic() + 25
    child.stdin.write(b'{"id":"commands","type":"get_commands"}\n')
    child.stdin.flush()
    try:
        while status is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"RPC timeout: {errors[-2000:]!r}")
            ready = selector.select(remaining)
            if not ready:
                raise AssertionError(f"RPC deadline: {errors[-2000:]!r}")
            for key, _ in ready:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    raise AssertionError(f"RPC exited {child.poll()}: {errors[-2000:]!r}")
                if key.data == "stderr":
                    errors = (errors + chunk)[-4096:]
                    continue
                pending += chunk
                assert len(pending) < 1024 * 1024
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not line:
                        continue
                    message = json.loads(line)
                    if message.get("id") == "commands":
                        assert message["success"], message
                        commands = [command["name"] for command in message["data"]["commands"]]
                        assert "mcp-status" in commands and "mcp-approve" in commands, (
                            commands,
                            errors,
                        )
                        child.stdin.write(
                            b'{"id":"status","type":"prompt","message":"/mcp-status"}\n'
                        )
                        child.stdin.flush()
                    if (
                        message.get("type") == "extension_ui_request"
                        and message.get("method") == "notify"
                    ):
                        value = message.get("message", "")
                        if value.startswith("MCP:\n"):
                            status = value
        assert "user/fixture: trust_required; calls=unavailable" in status, status
        assert not marker.exists(), "Unapproved MCP process ran"
        print(
            json.dumps(
                {
                    "ok": True,
                    "root": str(root),
                    "commands": commands,
                    "status": status,
                    "network": "seccomp-kill-on-network; negative control SIGSYS",
                    "extension": str(extension),
                    "unapprovedChild": False,
                    "ancestor": ancestor,
                }
            )
        )
    finally:
        selector.close()
        child.stdin.close()
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True))
