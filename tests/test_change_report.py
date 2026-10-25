"""The change-report kind and its declared evidence, through the real send path."""

import os
from dataclasses import replace
import subprocess
from pathlib import Path

import pytest

from agent_comms.channel_targets import CommandTarget
from agent_comms.registry_document import NoCommanderProject
from agent_comms.change_report import ChangedFact, ChangeReport, FileLine, OrderRoots
from agent_comms.errors import MissingSourceLine
from agent_comms.repository_name import RepositoryName
from source_revision_fixture import SourceRepository
from agent_comms.read_basis import NamedViewer
from agent_comms.messages import (
    AckKind, AlertKind, ChangeReportKind, HandoffKind, InfoKind, Message,
    MessageKind, QuestionKind,
)
from agent_comms.threads import Thread


def git(worktree, *arguments):
    environment = dict(
        os.environ,
        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
    )
    return subprocess.run(
        ["git", "-C", str(worktree), *arguments],
        check=True, capture_output=True, env=environment,
    )


@pytest.fixture
def reporter_repo(tmp_path):
    """A registered sender whose worktree is a real git branch head."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    (worktree / "src").mkdir()
    (worktree / "src" / "demo.py").write_text("one\ntwo\nthree\n")
    git(worktree, "add", "src/demo.py")
    git(worktree, "commit", "-m", "declared source")
    return worktree


@pytest.fixture
def reporter(wired, reporter_repo):
    wired.registry.declare(Thread(name='reporter', tags=frozenset(), worktree=str(reporter_repo)))
    return wired


def report(repo):
    return ChangeReport(
        branch="main",
        facts=(ChangedFact(
            fact="the message kind declaration is single",
            owner=FileLine.from_wire("src/demo.py:2"),
            consumers=(FileLine.from_wire("src/demo.py:2"),),
            search_command="grep -rn 'fact' src",
            edits_before=3,
            edits_after=2,
        ),),
    )


class TestChangeReportKind:
    def test_send_requires_the_report_fields(self, reporter):
        with pytest.raises(ValueError):
            reporter.messaging.send("reporter", "fixer", "review this", ChangeReportKind).message.message_id

    def test_send_refuses_a_reference_absent_at_the_named_head(self, reporter):
        invalid = ChangeReport(
            branch="main",
            facts=(ChangedFact(
                fact="fact", owner=FileLine.from_wire("src/demo.py:9"),
                consumers=(FileLine.from_wire("src/demo.py:2"),),
                search_command="grep -rn fact src", edits_before=1, edits_after=1,
            ),),
        )
        with pytest.raises(ValueError):
            reporter.messaging.send("reporter", "fixer", "review this", ChangeReportKind, report=invalid).message.message_id

    def test_send_refuses_an_unknown_branch(self, reporter):
        invalid = ChangeReport(
            branch="ghost-branch",
            facts=(ChangedFact(
                fact="fact", owner=FileLine.from_wire("src/demo.py:2"),
                consumers=(FileLine.from_wire("src/demo.py:2"),),
                search_command="grep -rn fact src", edits_before=1, edits_after=1,
            ),),
        )
        with pytest.raises(ValueError):
            reporter.messaging.send("reporter", "fixer", "review this", ChangeReportKind, report=invalid).message.message_id

    def test_a_reference_naming_a_registered_repository_is_read_at_its_main(
            self, reporter_repo, tmp_path):
        other = SourceRepository.create(tmp_path / 'other', RepositoryName.from_wire('example/other'))
        roots = OrderRoots(reporter_repo, other.project)

        def naming(owner):
            return ChangeReport(branch="main", facts=(ChangedFact(
                fact="fact", owner=FileLine.from_wire(owner),
                consumers=(FileLine.from_wire("src/demo.py:3"),),
                search_command="grep -rn fact src", edits_before=1, edits_after=1,
            ),))

        naming("example/other:owner.py:2").require_references_exist(roots)
        with pytest.raises(MissingSourceLine, match='does not exist'):
            naming("example/other:owner.py:400").require_references_exist(roots)
        with pytest.raises(MissingSourceLine, match="'ghost' does not resolve"):
            replace(naming("example/other:owner.py:2"), branch="ghost").require_references_exist(roots)
        with pytest.raises(ValueError, match='no registered commander project'):
            naming("example/other:owner.py:2").require_references_exist(
                OrderRoots(reporter_repo, NoCommanderProject()))

    def test_declared_report_publishes_and_round_trips(self, reporter, reporter_repo):
        committed = reporter.messaging.send(
            "reporter", "fixer", "review this", ChangeReportKind, report=report(reporter_repo)).message
        stored, = list(reporter.views.history_page(CommandTarget.named("fixer"), viewer=NamedViewer("reporter")).messages)
        assert stored.report == report(reporter_repo)
        assert stored.type is ChangeReportKind
        assert (
            Message.from_wire(Message.from_message(committed).to_wire())
            == Message.from_message(stored)
        )

    def test_missing_fields_are_named_in_the_refusal(self):
        from agent_comms.field_codec import FieldCodec, MalformedRecord

        with pytest.raises(MalformedRecord):
            FieldCodec.decode(ChangeReport, {"branch": "main"})
        with pytest.raises(MalformedRecord):
            FieldCodec.decode(ChangedFact, {
                "fact": "f", "consumers": ["a.py:1"], "search_command": "grep x",
                "edits_before": 1, "edits_after": 1,
            })


class TestHandoffClaims:
    def test_claim_without_evidence_is_refused_naming_the_claims(self, wired):
        with pytest.raises(ValueError):
            wired.messaging.send("PR111", "fixer", "it is fixed", HandoffKind).message.message_id

    def test_a_bare_evidence_word_is_not_a_command(self, wired):
        with pytest.raises(ValueError):
            wired.messaging.send("PR111", "fixer", "I will find it; it is fixed", HandoffKind).message.message_id

    def test_claim_with_a_file_line_reference_publishes(self, wired):
        committed = wired.messaging.send(
            "PR111", "fixer", "it is fixed at src/x.py:12", HandoffKind).message
        assert committed.type is HandoffKind

    def test_claim_with_a_command_in_backticks_publishes(self, wired):
        committed = wired.messaging.send(
            "PR111", "fixer", "fixed; proof: `grep -rn kind src`", HandoffKind).message
        assert committed.type is HandoffKind

    def test_a_command_outside_backticks_is_not_evidence(self, wired):
        with pytest.raises(ValueError):
            wired.messaging.send("PR111", "fixer", "fixed; I ran grep everywhere", HandoffKind).message.message_id

    def test_plain_handoff_without_claims_publishes(self, wired):
        committed = wired.messaging.send(
            "PR111", "fixer", "handing over the thread", HandoffKind).message
        assert committed.type is HandoffKind


class TestKindDeclarations:
    def test_existing_wire_names_are_unchanged(self):
        kinds = {InfoKind: "info", QuestionKind: "question", AckKind: "ack",
                 HandoffKind: "handoff", AlertKind: "alert"}
        for kind, name in kinds.items():
            message = Message(sender="a", target="b", body="text", type=kind)
            assert message.to_wire()["type"] == name

    def test_stored_rows_without_reports_decode(self):
        stored = Message.from_wire({
            "seq": 3, "id": "ignored", "from": "a", "to": "b", "ts": 1.0,
            "text": "old handoff", "type": "handoff",
        })
        assert stored.type is HandoffKind
        with pytest.raises(ValueError):
            stored.report.require_references_exist(OrderRoots(Path(), NoCommanderProject()))

    def test_kind_selector_resolves_once(self):
        assert MessageKind.member_named("change-report") is ChangeReportKind
        assert MessageKind.field_value("alert") is AlertKind
        assert MessageKind.field_value(AlertKind) is AlertKind

    def test_file_line_scans_references_from_text(self):
        references = FileLine.scan("see src/a.py:12, org.py:3, `/home/ts/a.py:9` and plain seq:7",
                                   NoCommanderProject())
        assert [r.to_wire() for r in references] == ["src/a.py:12", "org.py:3", "/home/ts/a.py:9"]
