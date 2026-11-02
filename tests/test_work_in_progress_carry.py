"""The work-in-progress markers are declared once, in the compaction instruction; the reader's
pattern is derived from them and accepts only the three declared lines, in order."""

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


def test_the_reader_accepts_only_the_declared_lines_in_order():
    module = _patch_module()
    now, uncommitted, upcoming = module.WORK_IN_PROGRESS_MARKERS
    pattern = module.WORK_IN_PROGRESS_PATTERN
    assert re.search(pattern, f"{now}: a\n{uncommitted}: b\n{upcoming}: c")
    assert not re.search(pattern, f"{now}: a\n{upcoming}: b\n{uncommitted}: c")
    assert not re.search(pattern, f"{now}: a\n{upcoming}: c")


def test_labels_elsewhere_in_the_instruction_never_become_markers(monkeypatch):
    markers = _patch_module().WORK_IN_PROGRESS_MARKERS
    prose, read_text = INSTRUCTION.read_text(), Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self: "`Other:` unrelated\n\n" + prose
                        + "\n`Later:` unrelated\n" if self == INSTRUCTION else read_text(self))
    assert _patch_module().WORK_IN_PROGRESS_MARKERS == markers
