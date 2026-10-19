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
        tracked = set(run("git", "ls-tree", "-r", "--name-only", self.revision,
                          cwd=self.repo).splitlines())
        selected: set[str] = set()

        def include(value: str) -> None:
            if value and value not in (".", "..") and not value.startswith("/"):
                selected.update(p for p in tracked if p == value or p.startswith(value.rstrip("/") + "/"))

        def read(path: str) -> str:
            return run("git", "show", f"{self.revision}:{path}", cwd=self.repo)

        include("pyproject.toml")
        project = tomllib.loads(read("pyproject.toml"))
        build = project["tool"]["hatch"]["build"]
        wheel = build["targets"]["wheel"]
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
                        candidates = [parent / (module + ".py"), Path(module + ".py"),
                                      parent / module / "__init__.py"]
                    pending.extend(str(candidate) for candidate in candidates if str(candidate) in tracked)
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module = alias.name.replace(".", "/") + ".py"
                        pending.extend(candidate for candidate in (module, str(Path(path).parent / module))
                                       if candidate in tracked)
        # No second list of carry modules: a declared directory admits future steps.
        return selected


def affected(repo: Path, base: str, head: str) -> list[str]:
    changed = set(run("git", "diff", "--name-only", "--no-renames", base, head,
                      cwd=repo).splitlines())
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
        destination.mkdir(mode=0o700)
        command = ["bwrap", "--ro-bind", "/", "/", "--unshare-all", "--die-with-parent",
                   "/usr/bin/python3", "-I", "-B", "-", self.root]
        if self.transport:
            command = [*self.transport, shlex.join(command)]
        # bwrap has NO writable bind, tmpfs, dev or proc mount. SQLite cannot
        # update even its SHM file; WAL mode uses SQLite's read-only reader path.
        with log.open("wb") as errors, child(command, stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=errors) as process:
            process.stdin.write(SNAPSHOT.read_bytes())
            process.stdin.close()
            with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
                for entry in archive:
                    if not (entry.isfile() or entry.isdir()):
                        raise ValueError("Snapshot contains a link or special file")
                    archive.extract(entry, destination, filter="data")
            if process.wait() != 0:
                raise RuntimeError(f"{self.name} snapshot failed; see private log {log}")
        context = json.loads((destination / "context.json").read_text())
        if not (destination / "bus").is_dir():
            raise ValueError(f"{self.name} snapshot has no bus")
        return context


def prepare_home(home: Path, bus: Path, context: dict) -> None:
    home.mkdir(mode=0o700)
    # Preserve each machine's route selection, rebinding only its bus path.
    route = context["route"]
    if route is not None:
        if Path(route["root"]) != Path(context["root"]):
            raise ValueError("Machine's selected route does not select its specified bus")
        path = home / ".local/state/agent-comms/active-route.json"
        path.parent.mkdir(mode=0o700, parents=True)
        path.write_text(json.dumps({**route, "root": str(bus)}))
        path.chmod(0o600)
    pointer = home / ".local/share/agent-comms/current"
    pointer.parent.mkdir(mode=0o700, parents=True)
    # The candidate is built and rehearsed locally. The previous executable is
    # this machine's installed runtime; foreign executable paths are not portable.
    installed = Path.home() / ".local/share/agent-comms/current"
    pointer.symlink_to(installed.resolve(strict=True))
    uv = home / ".local/state/agent-comms/upgrade-bus/uv"
    uv.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    uv.symlink_to(Path.home() / ".local/state/agent-comms/upgrade-bus/uv")


@contextmanager
def checkout(repo: Path, revision: str, directory: Path):
    run("git", "worktree", "add", "--detach", directory, revision, cwd=repo)
    try:
        yield directory
    finally:
        run("git", "worktree", "remove", "--force", directory, cwd=repo)


def rehearse(target: Path, previous: Path, head: str, bus: Path, home: Path,
             python: Path, log: Path, work: Path) -> dict:
    environment = {**os.environ, "HOME": str(home), "TMPDIR": str(work),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    # The target owns its CLI. Discover the repository option from that CLI,
    # rather than importing/reimplementing its rehearsal or carry behavior.
    help_text = run(python, target / INSTALLER, "--help", cwd=target, env=environment)
    repo_option = "--repo" if "--repo " in help_text else "--checkout"
    command = [python, "-X", "faulthandler", target / INSTALLER, repo_option, previous,
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
        run("gh", "api", "--method", "PUT", f"repos/{repository}/branches/main/protection",
            "--input", "-", input=json.dumps(payload))
    else:
        checks = json.loads(response.stdout).get("required_status_checks") or {}
        required = checks.get("checks", [{"context": name} for name in checks.get("contexts", [])])
        if not any(check["context"] == CONTEXT for check in required):
            required.append({"context": CONTEXT})
        run("gh", "api", "--method", "PATCH",
            f"repos/{repository}/branches/main/protection/required_status_checks", "--input", "-",
            input=json.dumps({"strict": checks.get("strict", False), "checks": required}))


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
    repository = run("gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
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
                                home = directory / "home"
                                prepare_home(home, bus, context)
                                report["machines"][machine.name] = rehearse(
                                    target, previous, head, bus, home, args.python, log, work)
                        except Exception as error:
                            report["machines"][machine.name] = {"passed": False, "error": str(error)}
            if not all(result["passed"] for result in report["machines"].values()):
                raise RuntimeError("At least one machine failed; see private receipt")
            report["result"] = "both rehearsals passed"
        report["passed"] = True
    except Exception as error:
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
