"""Build one fresh candidate from the final committed Toad dependency graph."""

import argparse
import json
from pathlib import Path
import subprocess
import tomllib


def git_bytes(repo, revision, path):
    return subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata-repo", type=Path, required=True)
    parser.add_argument("--core", required=True)
    parser.add_argument("--toad", required=True)
    parser.add_argument("--textual", required=True)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args()
    assert not args.stage.exists(), "Never modify an existing candidate or published prefix"
    assert args.stage.parent == Path("/home/ts/.local/share/agent-comms")
    assert len(args.core) == len(args.toad) == len(args.textual) == 40
    for name in ("pyproject.toml", "uv.lock"):
        assert (args.metadata_repo / name).read_bytes() == git_bytes(
            args.metadata_repo, args.toad, name
        ), f"Metadata changed from the declared immutable Toad source: {name}"
    project = tomllib.loads(git_bytes(args.metadata_repo, args.toad, "pyproject.toml").decode())
    sources = project["tool"]["uv"]["sources"]
    assert sources["agent-comms"]["rev"] == args.core
    assert sources["textual"]["rev"] == args.textual
    args.evidence.mkdir(parents=True, exist_ok=True)
    exported = subprocess.check_output([
        "uv", "export", "--directory", str(args.metadata_repo), "--frozen",
        "--no-dev", "--no-hashes", "--no-emit-project", "--format", "requirements-txt",
    ], text=True)
    requirements = args.evidence / "paired-requirements.txt"
    requirements.write_text(exported + "\nbatrachian-toad @ git+https://github.com/"
                            f"OpenHCSDev/toad.git@{args.toad}\n")
    with (args.evidence / "paired-build.log").open("w") as log:
        for command in (
            ["uv", "venv", "--python", str(args.python), str(args.stage)],
            ["uv", "pip", "install", "--python", str(args.stage / "bin/python"),
             "-r", str(requirements)],
            ["uv", "pip", "check", "--python", str(args.stage / "bin/python")],
        ):
            subprocess.run(command, check=True, stdout=log, stderr=log)
    freeze = subprocess.check_output([
        "uv", "pip", "freeze", "--python", str(args.stage / "bin/python")
    ], text=True)
    (args.evidence / "paired-installed-freeze.txt").write_text(freeze)
    report = {"stage": str(args.stage), "core": args.core, "toad": args.toad,
              "textual": args.textual, "distribution_count": len(freeze.splitlines()),
              "dependency_source": "final committed Toad uv.lock, exported without dev dependencies",
              "resolver": "normal uv pip install, no source or dependency overrides",
              "public_effects": 0, "scope": "packaging only; source/native/private-root admission follows"}
    (args.evidence / "package-build-receipt.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
