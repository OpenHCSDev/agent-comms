"""The stated work-in-progress format has one declaration; prose and reader agree."""

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "stack/native" / "patch-native-compaction-wip.py"
INSTRUCTION = ROOT / "src" / "agent_comms" / "instructions" / "coordination.md"


def _patch_module():
    spec = importlib.util.spec_from_file_location("patch_native_compaction_wip", PATCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_instruction_and_the_compaction_reader_declare_one_format(monkeypatch):
    module = _patch_module()
    prose = INSTRUCTION.read_text()
    # The coordination instruction names the three markers, in order, backticked.
    stated = re.findall(r"`([A-Za-z]+):`", prose)
    assert stated == list(module.WORK_IN_PROGRESS_MARKERS), prose
    sample = "\n".join(f"{marker}: detail" for marker in stated)
    assert re.search(module.WORK_IN_PROGRESS_PATTERN, sample)
    # A different marker order or a missing middle line never matches.
    assert not re.search(module.WORK_IN_PROGRESS_PATTERN,
                         "Now: a\nNext: b\nUncommitted: c")
    assert not re.search(module.WORK_IN_PROGRESS_PATTERN, "Now: a\nUncommitted: c")
    # Labels elsewhere in the instruction never become compaction markers.
    read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self: "`Other:` unrelated\n\n" + prose
                        + "\n`Later:` unrelated\n" if self == INSTRUCTION else read_text(self))
    assert _patch_module().WORK_IN_PROGRESS_MARKERS == module.WORK_IN_PROGRESS_MARKERS


def test_the_prepared_reader_carries_the_declared_markers():
    module = _patch_module()
    markers = module.WORK_IN_PROGRESS_MARKERS
    assert re.search(module.WORK_IN_PROGRESS_PATTERN,
                     "Now: adding an event class\nUncommitted: test edits\nNext: run tests")
    # The dirty annotation names the declared uncommitted marker, not a copy.
    assert module.UNCOMMITTED_MARKER == markers[1] == "Uncommitted"
