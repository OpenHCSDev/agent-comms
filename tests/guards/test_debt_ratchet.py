"""Exercise the command against real Git commits, without mocking Git or ASTs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.refactor_guard



class Repository:
    def __init__(self, path: Path, root: str):
        self.path = path
        self.root = root
        self.git("init", "-q")
        self.git("config", "user.email", "ratchet@example.invalid")
        self.git("config", "user.name", "Ratchet test")

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.path), *args], text=True, timeout=10
        ).strip()

    def commit(self, files: dict[str, str | None]) -> str:
        for name, source in files.items():
            path = self.path / self.root / name
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
            [str(Path(sys.executable).with_name("agent-comms-ratchet")), "--root", self.root, "--base", base, "--head", head],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode in (0, 1), result.stderr
        return result.returncode, json.loads(result.stdout)


@pytest.fixture(params=["src/agent_comms", "src/toad"])
def repo(tmp_path: Path, request) -> Repository:
    return Repository(tmp_path, request.param)


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


def test_other_source_root_does_not_enter_report(repo: Repository) -> None:
    base = repo.commit({"kept.py": "value = 1\n"})
    outside = repo.path / "unrelated.py"
    outside.write_text('value = type(x) is int and a and b and row["key"]\n')
    head = repo.commit({"kept.py": "value = 2\n"})
    status, report = repo.compare(base, head)
    assert status == 0
    assert report["paths"] == [f"{repo.root}/kept.py"]


def test_ratchet_has_one_packaged_owner() -> None:
    from agent_comms import debt_ratchet
    assert not (Path(__file__).resolve().parents[2] / "tools/debt_ratchet.py").exists()
    assert "src/agent_comms/" not in Path(debt_ratchet.__file__).read_text()


def test_class_growth_cannot_be_cancelled_by_another_class(repo: Repository) -> None:
    base = repo.commit({"classes.py": "class Growing:\n    pass\n\nclass Shrinking:\n    first = 1\n    second = 2\n"})
    head = repo.commit({"classes.py": "class Growing:\n    first = 1\n    second = 2\n\nclass Shrinking:\n    pass\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"ClassSize:{repo.root}/classes.py::Growing"] == 1
    assert report["delta"][f"ClassSize:{repo.root}/classes.py::Shrinking"] == -1


def test_class_move_keeps_baseline_and_new_owner_starts_baseline(repo: Repository) -> None:
    base = repo.commit({"old.py": "class Existing:\n    pass\n"})
    moved = repo.commit({"old.py": None, "new.py": "class Existing:\n    pass\n\nclass NewOwner:\n    value = 1\n"})
    status, report = repo.compare(base, moved)
    assert status == 0
    assert report["delta"][f"ClassSize:{repo.root}/new.py::Existing"] == 0
    assert report["base"][f"ClassSize:{repo.root}/new.py::NewOwner"] is None
    assert report["delta"][f"ClassSize:{repo.root}/new.py::NewOwner"] is None
    grown = repo.commit({"new.py": "class Existing:\n    pass\n\nclass NewOwner:\n    value = 1\n    more = 2\n"})
    assert repo.compare(moved, grown)[0] == 1


def test_moved_class_growth_is_rejected(repo: Repository) -> None:
    base = repo.commit({"old.py": "class Existing:\n    pass\n"})
    head = repo.commit({"old.py": None, "new.py": "class Existing:\n    value = 1\n    more = 2\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"ClassSize:{repo.root}/new.py::Existing"] == 1


def test_nested_and_duplicate_named_classes_keep_separate_baselines(repo: Repository) -> None:
    base = repo.commit({"one.py": "class Outer:\n    class Inner:\n        pass\n", "two.py": "class Inner:\n    pass\n"})
    head = repo.commit({"one.py": "class Outer:\n    class Inner:\n        pass\n", "two.py": "class Inner:\n    value = 1\n    more = 2\n"})
    status, report = repo.compare(base, head)
    assert status == 1
    assert report["delta"][f"ClassSize:{repo.root}/one.py::Outer.Inner"] == 0
    assert report["delta"][f"ClassSize:{repo.root}/two.py::Inner"] == 1
