"""Exercise the command against real Git commits, without mocking Git or ASTs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard
SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "debt_ratchet.py"


class Repository:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q")
        self.git("config", "user.email", "ratchet@example.invalid")
        self.git("config", "user.name", "Ratchet test")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.path), *args], text=True, timeout=10
        ).strip()

    def commit(self, files: dict[str, str | None]) -> str:
        for name, source in files.items():
            path = self.path / "src" / "agent_comms" / name
            if source is None:
                path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(source)
        self.git("add", "--all")
        self.git("-c", "commit.gpgsign=false", "commit", "-qm", "test", "--allow-empty")
        return self.git("rev-parse", "HEAD")

    def compare(self, base: str, head: str) -> tuple[int, dict]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--base", base, "--head", head],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode, json.loads(result.stdout)


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path)


def test_added_type_check_fails(repo: Repository) -> None:
    base = repo.commit({"check.py": 'value = row["a"]\n'})
    head = repo.commit({"check.py": "value = type(x) is int\n"})
    status, report = repo.compare(base, head)
    assert status == 1  # A different measure's reduction cannot cancel this increase.
    assert report["delta"] == {
        "TypeIdentity": 1,
        "LongBooleanChain": 0,
        "StringSubscript": -1,
    }


def test_moves_between_files_pass(repo: Repository) -> None:
    source = 'value = type(x) is not int and a and b and row["key"]\n'
    base = repo.commit({"from.py": source, "other.py": "a = 1\n"})
    moved = repo.commit({"from.py": None, "nested/to.py": source})
    combined = repo.commit({"nested/to.py": None, "other.py": "a = 1\n" + source})
    for head in (moved, combined):
        status, report = repo.compare(base, head)
        assert status == 0
        assert (
            report["base"]
            == report["head"]
            == {
                "TypeIdentity": 1,
                "LongBooleanChain": 1,
                "StringSubscript": 1,
            }
        )


def test_reduction_passes(repo: Repository) -> None:
    base = repo.commit({"removed.py": 'value = type(x) is int and a and b and row["key"]\n'})
    head = repo.commit({"removed.py": None})
    status, report = repo.compare(base, head)
    assert status == 0
    assert report["delta"] == {
        "TypeIdentity": -1,
        "LongBooleanChain": -1,
        "StringSubscript": -1,
    }


def test_new_file_smells_count(repo: Repository) -> None:
    base = repo.commit({})
    head = repo.commit({"new.py": """
value = type(x) is int is not type(y)
other = type(z) == int
chain = a or b or c or d
short = a and b and c
read = row["key"]
row['other'] = read
numeric = row[0]
text = 'type(x) is int and a and b and row["not code"]'
# type(x) is int and a and b and row["not code"]
"""})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["base"] == {name: 0 for name in report["head"]}
    assert report["head"] == {
        "TypeIdentity": 2,
        "LongBooleanChain": 1,
        "StringSubscript": 2,
    }
