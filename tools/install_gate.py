#!/usr/bin/env python3
"""Local required install check; private bus copies never go to GitHub.

    python tools/install_gate.py --pr 123
    python tools/install_gate.py --revision origin/main --base origin/main^

The second form is a forced local proof and never posts a commit status.
The PR form posts install-rehearsal for the PR's exact head. Configure that
context as required with --require-status (an explicit repository-admin action).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTEXT = "install-rehearsal"
SNAPSHOT = Path(__file__).with_name("install_snapshot.py")
SNAPSHOT_SOURCE = SNAPSHOT.read_bytes()
INSTALLER = Path("tools/upgrade_bus.py")
DEFAULT_SCRATCH = Path.home() / ".cache/agent-scratch/pi-install-gate"


def run(*command, cwd=REPO, **kwargs) -> str:
    return subprocess.run(list(map(str, command)), cwd=cwd, check=True,
                          stdout=subprocess.PIPE, text=True, **kwargs).stdout.strip()


@dataclass(frozen=True)
class InstallSurface:
    """The distribution and the installer's declared source/file dependencies."""
    repo: Path
    revision: str

    def paths(self) -> set[str]:
        tracked = set(filter(None, run("git", "ls-tree", "-r", "-z", "--name-only", self.revision,
                                       cwd=self.repo).split("\0")))
        selected: set[str] = set()
        members: dict[str, set[str]] = {}
        for path in tracked:
            for prefix in (Path(path), *Path(path).parents):
                members.setdefault(str(prefix), set()).add(path)

        def include(value: str) -> None:
            value = value.rstrip("/")
            if value not in (".", ".."):
                selected.update(members.get(value, ()))

        def read(path: str) -> str:
            return run("git", "show", f"{self.revision}:{path}", cwd=self.repo)

        include("pyproject.toml")
        include(".gitmodules")
        project = tomllib.loads(read("pyproject.toml"))
        build = project["tool"]["hatch"]["build"]
        wheel = build["targets"]["wheel"]
        source_roots = {Path(path).parent for path in wheel["packages"]}
        for path in wheel["packages"]:
            include(path)
        for path in wheel.get("force-include", {}):
            include(path)
        include(str(Path(__file__).resolve().relative_to(REPO)))
        include(str(SNAPSHOT.relative_to(REPO)))
        pending = [str(INSTALLER)]
        for hook in build.get("hooks", {}).values():
            if "path" in hook:
                pending.append(hook["path"])
        seen: set[str] = set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if path not in tracked:
                raise ValueError(f"Missing install dependency at {self.revision}: {path}")
            include(path)
            tree = ast.parse(read(path), filename=path)
            # Repository paths are declared in the installer/build owner, including
            # the carry directory and stack directory. Select their whole contents.
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    include(node.value)
                if isinstance(node, ast.ImportFrom):
                    parent = Path(path).parent
                    if node.level:
                        for _ in range(node.level - 1):
                            parent = parent.parent
                        module = parent / (node.module or "").replace(".", "/")
                        candidates = [module.with_suffix(".py"), module / "__init__.py"]
                    else:
                        module = (node.module or "").replace(".", "/")
                        candidates = [root / candidate for root in (parent, Path(), *source_roots)
                                      for candidate in (Path(module + ".py"), Path(module) / "__init__.py")]
                    # `from package import child` may import a module, not a symbol.
                    stems = [candidate.parent for candidate in candidates if candidate.name == "__init__.py"]
                    candidates.extend(stem / (alias.name + ".py") for stem in stems for alias in node.names)
                    pending.extend(str(candidate) for candidate in candidates if str(candidate) in tracked)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name.replace(".", "/") + ".py"
                        pending.extend(str(root / module) for root in (Path(path).parent, Path(), *source_roots)
                                       if str(root / module) in tracked)
        # No second list of carry modules: a declared directory admits future steps.
        return selected


def affected(repo: Path, base: str, head: str) -> list[str]:
    changed = set(filter(None, run("git", "diff", "--name-only", "-z", "--no-renames", base, head,
                                  cwd=repo).split("\0")))
    return sorted(changed & (InstallSurface(repo, base).paths() | InstallSurface(repo, head).paths()))


def terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


@contextmanager
def child(command, **kwargs):
    process = subprocess.Popen(list(map(str, command)), start_new_session=True, **kwargs)
    try:
        yield process
    finally:
        terminate(process)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


@dataclass(frozen=True)
class Machine:
    name: str
    root: str
    transport: tuple[str, ...] = ()

    def capture(self, destination: Path, log: Path) -> dict:
        from agent_comms.storage_rules import StorageRule

        destination.mkdir(mode=0o700)
        floor = StorageRule.load(Path.home() / ".agent-comms").floor_gib * 2**30
        captured = 0
        command = ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent",
                   "/usr/bin/python3", "-I", "-B", "-", self.root]
        if self.transport:
            command = [*self.transport, shlex.join(command)]
        # bwrap has NO writable bind, tmpfs, dev or proc mount. SQLite cannot
        # update even its SHM file; WAL mode uses SQLite's read-only reader path.
        with log.open("wb") as errors, child(command, stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=errors) as process:
            process.stdin.write(SNAPSHOT_SOURCE)
            process.stdin.close()
            with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
                for entry in archive:
                    if not (entry.isfile() or entry.isdir()):
                        raise ValueError("Snapshot contains a link or special file")
                    # Reserve the second copy the installer itself will make,
                    # using the storage owner's configured free-space floor.
                    if shutil.disk_usage(destination).free < captured + 2 * entry.size + floor:
                        raise RuntimeError(f"Insufficient room for snapshot and rehearsal at {destination}")
                    archive.extract(entry, destination, filter="data")
                    captured += entry.size
            if process.wait() != 0:
                raise RuntimeError(f"{self.name} snapshot failed; see private log {log}")
        context = json.loads((destination / "context.json").read_text())
        if not (destination / "bus").is_dir():
            raise ValueError(f"{self.name} snapshot has no bus")
        return context


def prepare_home(home: Path, bus: Path, context: dict) -> Path:
    home.mkdir(mode=0o700, exist_ok=True)
    # Preserve the bus's position within its original home, not just an
    # explicit route file. This keeps default-root writer checks meaningful.
    original, source_home = Path(context["root"]), Path(context["home"])
    if original.is_relative_to(source_home):
        relative = original.relative_to(source_home)
        if relative == Path(".") or ".." in relative.parts:
            raise ValueError("Bus must be a safe subdirectory of the source home")
        selected = home / relative
        from agent_comms.storage_rules import StorageRule
        size = sum(path.stat().st_size for path in bus.rglob("*") if path.is_file())
        floor = StorageRule.load(Path.home() / ".agent-comms").floor_gib * 2**30
        if shutil.disk_usage(home).free < 2 * size + floor:
            raise RuntimeError("Insufficient room to preserve the bus's home-relative position")
        selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(bus, selected)
        bus = selected
    # Preserve each machine's route selection, rebinding only its bus path.
    path = home / ".local/state/agent-comms/active-route.json"
    path.unlink(missing_ok=True)
    route = context["route"]
    if route is not None:
        if Path(route["root"]) != Path(context["root"]):
            raise ValueError("Machine's selected route does not select its specified bus")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({**route, "root": str(bus)}))
        path.chmod(0o600)
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    pointer.unlink(missing_ok=True)
    # The candidate is built and rehearsed locally. The previous executable is
    # this machine's installed runtime; foreign executable paths are not portable.
    installed = Path.home() / ".local/share/agent-comms/current"
    pointer.symlink_to(installed.resolve(strict=True))
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv.unlink(missing_ok=True)
    uv.symlink_to(Path.home() / ".local/state/agent-comms/upgrade-bus/uv")
    # Native preparation deliberately installs its pinned MCP dependencies
    # offline. A synthetic HOME must retain the existing package-manager cache.
    cache = Path(run("npm", "config", "get", "cache"))
    if not (home / ".npm").exists() and cache.is_dir():
        shutil.copytree(cache, home / ".npm", symlinks=True)
    return bus


@contextmanager
def checkout(repo: Path, revision: str, directory: Path):
    run("git", "worktree", "add", "--detach", directory, revision, cwd=repo)
    try:
        yield directory
    finally:
        run("git", "worktree", "remove", "--force", directory, cwd=repo)


def rehearse(target: Path, previous: Path, head: str, bus: Path, home: Path,
             python: Path, log: Path, work: Path) -> dict:
    from agent_comms.launch_runtime import RestartEnvironment, ThreadLaunch

    environment = ThreadLaunch.strip(dict(os.environ))
    RestartEnvironment.strip(environment, "root", "virtual_env", "pythonpath")
    # A rehearsal is not a child of the gate agent's active turn or tool server.
    for name in ("AGENT_COMMS_PROJECT_SOCKET", "AGENT_COMMS_PROJECT_REQUEST",
                 "PI_SESSION_ID", "PI_SESSION_FILE"):
        environment.pop(name, None)
    uv = Path.home() / ".local/state/agent-comms/upgrade-bus/uv"
    uv_cache = Path(run(uv, "cache", "dir"))
    uv_cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment.update(HOME=str(home), TMPDIR=str(work), PYTHONDONTWRITEBYTECODE="1",
                       XDG_CACHE_HOME=str(home / ".cache"), XDG_CONFIG_HOME=str(home / ".config"),
                       XDG_STATE_HOME=str(home / ".local/state"), UV_CACHE_DIR=str(uv_cache),
                       npm_config_cache=str(home / ".npm"))
    common = Path(run("git", "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=target))
    sandbox = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--die-with-parent"]
    for directory in dict.fromkeys((work, home, bus.parent, common, uv_cache)):
        sandbox.extend(("--bind", str(directory), str(directory)))
    # The installer may write only private builds/copies, Git metadata and
    # uv's existing shared package cache (not installed runtime packages).
    # Absolute paths retained inside sessions cannot mutate the original bus.
    # The target owns its CLI; never import/reimplement its rehearsal behavior.
    help_text = run(*sandbox, python, target / INSTALLER, "--help", cwd=target, env=environment)
    repo_option = "--repo" if "--repo " in help_text else "--checkout"
    command = [*sandbox, python, "-X", "faulthandler", target / INSTALLER, repo_option, previous,
               "--root", bus, "--to", head, "--installer", "pi-install-gate", "--rehearse"]
    with log.open("w") as output, child(command, cwd=target, env=environment,
                                         stdout=output, stderr=subprocess.STDOUT) as process:
        code = process.wait()
    # Output can include bus history. Never upload it or print it into a PR.
    if code:
        raise RuntimeError(f"Rehearsal exit {code}; see private log {log}")
    text = log.read_text()
    decoder = json.JSONDecoder()
    result = None
    for offset, character in enumerate(text):
        if character == "{":
            try:
                candidate, end = decoder.raw_decode(text, offset)
            except json.JSONDecodeError:
                continue
            if not text[end:].strip():
                result = candidate
                break
    if not isinstance(result, dict) or not isinstance(result.get("rehearsal"), dict):
        raise RuntimeError(f"Installer did not return a rehearsal receipt; see {log}")
    rehearsal = result["rehearsal"]
    if "unchanged" in rehearsal or not {"files", "read", "steps"} <= rehearsal.keys():
        raise RuntimeError(f"Installer skipped the actual rehearsal; see {log}")
    return {"passed": True, "files": rehearsal["files"], "read": rehearsal["read"]}


def status(repository: str, head: str, state: str, description: str) -> None:
    run("gh", "api", "--method", "POST", f"repos/{repository}/statuses/{head}",
        "-f", f"state={state}", "-f", f"context={CONTEXT}", "-f", f"description={description}")


def api_payload(method: str, endpoint: str, payload: dict) -> None:
    # Give gh a bounded, explicit JSON request file.
    with tempfile.NamedTemporaryFile(mode="w", dir=REPO, prefix=".install-gate-api-") as request:
        json.dump(payload, request)
        request.flush()
        run("gh", "api", "--method", method, endpoint, "--input", request.name)


def require_status(repository: str) -> None:
    # Preserve existing protection. Never replace review/admin/restriction policy.
    response = subprocess.run(["gh", "api", f"repos/{repository}/branches/main/protection"],
                              capture_output=True, text=True)
    if response.returncode:
        error = json.loads(response.stdout or "{}")
        if error.get("status") != "404":
            raise RuntimeError(response.stderr or response.stdout)
        payload = {"required_status_checks": {"strict": True, "contexts": [CONTEXT]},
                   "enforce_admins": True, "required_pull_request_reviews": None,
                   "restrictions": None}
        api_payload("PUT", f"repos/{repository}/branches/main/protection", payload)
    else:
        checks = json.loads(response.stdout).get("required_status_checks") or {}
        required = checks.get("checks", [{"context": name} for name in checks.get("contexts", [])])
        if any(check["context"] == CONTEXT for check in required):
            return
        required.append({"context": CONTEXT})
        api_payload("PATCH", f"repos/{repository}/branches/main/protection/required_status_checks",
                    {"strict": checks.get("strict", False), "checks": required})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int)
    mode.add_argument("--revision")
    mode.add_argument("--require-status", action="store_true")
    parser.add_argument("--base")
    parser.add_argument("--repo", type=Path, default=REPO)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    args = parser.parse_args()
    repository = run("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner", cwd=args.repo)
    if args.require_status:
        require_status(repository)
        return 0
    if args.pr:
        pr = json.loads(run("gh", "pr", "view", args.pr, "--repo", repository,
                            "--json", "headRefOid,baseRefOid,isCrossRepository"))
        if pr["isCrossRepository"]:
            raise ValueError("Gate executes private-data rehearsals only for reviewed same-repository PRs")
        head, base = pr["headRefOid"], pr["baseRefOid"]
        status(repository, head, "pending", "Checking source ownership and both machines")
    else:
        head = run("git", "rev-parse", f"{args.revision}^{{commit}}", cwd=args.repo)
        base = run("git", "rev-parse", f"{args.base or head + '^'}^{{commit}}", cwd=args.repo)
    report = {"head": head, "base": base, "machines": {}}
    args.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.umask(0o077)
    receipt = args.scratch / f"receipt-{head[:12]}-{os.getpid()}.json"
    try:
        if args.pr:
            run("git", "fetch", "-q", "origin", base, head, cwd=args.repo)
        report["affected"] = affected(args.repo, base, head)
        if args.pr and not report["affected"]:
            report["result"] = "outside install/runtime distribution"
        else:
            machines = (Machine("laptop", str(Path.home() / ".agent-comms")),
                        Machine("desktop", "/var/tmp/agent-comms-live-20260927-wzjtqhza",
                                ("ssh", "-i", str(Path.home() / ".ssh/openhcs_laptop_ed25519"),
                                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "ts@192.168.2.11")))
            # Repository worktrees must remain on persistent /home/ts/wt, not TMPDIR.
            work_root = Path.home() / "wt"
            report["machines"] = {machine.name: {"passed": False, "error": "Not completed"}
                                  for machine in machines}
            with tempfile.TemporaryDirectory(dir=work_root, prefix="install-gate-") as source_work:
                work = Path(source_work)
                with checkout(args.repo, base, work / "base") as previous, \
                     checkout(args.repo, head, work / "target") as target:
                    for machine in machines:
                        log = receipt.with_name(receipt.stem + f"-{machine.name}.log")
                        try:
                            with tempfile.TemporaryDirectory(dir=args.scratch,
                                                             prefix=machine.name + "-") as directory:
                                directory = Path(directory)
                                context = machine.capture(directory / "snapshot", log)
                                bus = directory / "snapshot/bus"
                                home = work / "home"
                                bus = prepare_home(home, bus, context)
                                try:
                                    report["machines"][machine.name] = rehearse(
                                        target, previous, head, bus, home, args.python, log, work)
                                finally:
                                    if bus.is_relative_to(home):
                                        shutil.rmtree(bus)
                        except Exception as error:
                            report["machines"][machine.name] = {"passed": False, "error": str(error)}
            if not all(result["passed"] for result in report["machines"].values()):
                raise RuntimeError("At least one machine failed; see private receipt")
            report["result"] = "both rehearsals passed"
        report["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        report.update(passed=False, error=str(error))
    finally:
        receipt.write_text(json.dumps(report, indent=2) + "\n")
    if args.pr:
        status(repository, head, "success" if report["passed"] else "failure",
               "Both rehearsals passed" if report["passed"] and report["machines"] else
               "Outside install/runtime distribution" if report["passed"] else "Install rehearsal failed; local receipt retained")
    print(json.dumps({"passed": report["passed"], "machines": report["machines"],
                      "receipt": str(receipt)}, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    def stopped(signum, frame):
        raise KeyboardInterrupt(f"Stopped by signal {signum}")
    signal.signal(signal.SIGTERM, stopped)
    raise SystemExit(main())
