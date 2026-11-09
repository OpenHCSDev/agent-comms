"""Rule-clause source identity, including the recurrence consumer that refused the edit."""

from pathlib import Path

import pytest

from agent_comms.projects.change_report import FileLine
from agent_comms.projects.slop import DeclarationDerivation


@pytest.fixture
def rule_document(tmp_path: Path) -> Path:
    path = tmp_path / "rules.md"
    path.write_text(
        "# Rules\n"
        "Opening text.\n"
        "\n"
        "- **First rule.** Start here.\n"
        "  This line continues the first rule.\n"
        "\n"
        "- **Second rule.** A different obligation.\n"
        "  This line continues the second rule.\n"
    )
    return path


@pytest.mark.parametrize("line,start,stop,name", [
    (4, 4, 7, "First rule."),
    (5, 4, 7, "First rule."),
    (6, 4, 7, "First rule."),
    (7, 7, 9, "Second rule."),
    (8, 7, 9, "Second rule."),
])
def test_markdown_rule_clause_owns_its_continuations(
    rule_document: Path, line: int, start: int, stop: int, name: str,
) -> None:
    declaration = FileLine(rule_document.name, line).declaration(rule_document.parent)
    assert declaration.at.line == start
    assert declaration.lines == range(start, stop)
    assert declaration.name == name


def test_recurrence_distinguishes_rules_but_keeps_the_same_rule(rule_document: Path) -> None:
    root = rule_document.parent
    first = FileLine(rule_document.name, 4)
    continuation = FileLine(rule_document.name, 5)
    second = FileLine(rule_document.name, 7)
    assert first.declaration(root) == continuation.declaration(root)
    assert first.declaration(root) != second.declaration(root)
    assert DeclarationDerivation.recurrence_refusal(second, first, root) == ""
    assert DeclarationDerivation.recurrence_refusal(continuation, first, root)


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_fenced_examples_do_not_split_rule_clauses(tmp_path: Path, fence: str) -> None:
    path = tmp_path / "examples.md"
    path.write_text(
        "- **First rule.** Includes a code example.\n"
        f"{fence}\n"
        "- **Not a rule.** This is example data.\n"
        f"{fence}\n"
        "  Still the first rule.\n"
        "- **Second rule.** Starts here.\n"
    )
    first = FileLine(path.name, 1).declaration(tmp_path)
    assert first.lines == range(1, 6)
    assert FileLine(path.name, 3).declaration(tmp_path) == first
    assert FileLine(path.name, 5).declaration(tmp_path) == first
    assert FileLine(path.name, 6).declaration(tmp_path) != first


def test_non_rule_text_keeps_the_file_fallback(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("Some prose.\nMore prose.\n")
    assert FileLine(path.name, 1).declaration(tmp_path) == FileLine(path.name, 2).declaration(tmp_path)


def test_python_keeps_its_innermost_declaration(tmp_path: Path) -> None:
    path = tmp_path / "owner.py"
    path.write_text("class Owner:\n    def action(self):\n        return 1\n")
    declaration = FileLine(path.name, 3).declaration(tmp_path)
    assert declaration.name == "action"
    assert declaration.lines == range(2, 4)
