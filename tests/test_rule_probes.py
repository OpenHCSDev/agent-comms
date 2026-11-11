from pathlib import Path

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.projects.repo import Repo
from agent_comms.projects.repository_name import RepositoryName
from agent_comms.projects.rule_probes import FlinchProbe, HuntProbe, PriorProbe, Probe, ProbeContext
from agent_comms.team.rule_deal import AuditDue

HIS = ("cam we use knheritsmce rather than a flag",)


@pytest.fixture
def context() -> ProbeContext:
    return ProbeContext(HIS, Path.cwd(),
                        copied_from=("a rule says reach for a parent class before a flag",))


def probe(**answer: str) -> Probe:
    return FieldCodec.decode(Probe, answer)


def test_a_prior_must_quote_him_and_cite_a_result_the_papers_state(context):
    good = probe(probe="prior", words="rather than a flag", refuted_by="Corollary 4.12",
                 prior="a boolean felt lighter than one more base in the list")
    assert good.fault(context) == ""
    assert "Tristan's recorded prompts" in probe(
        probe="prior", words="use a flag", refuted_by="Corollary 4.12",
        prior="a boolean felt lighter than one more base in the list").fault(context)
    assert "numbered result" in probe(
        probe="prior", words="rather than a flag", refuted_by="Theorem 9.99",
        prior="a boolean felt lighter than one more base in the list").fault(context)


def test_a_hunt_is_a_slop_statement_whose_lines_exist_here(context):
    at = "src/agent_comms/projects/rule_probes.py:1"
    good = probe(probe="hunt", pattern="IMPL-1", fact="the probe kinds are listed by hand twice",
                 at=at, derive="declaration", by=at)
    assert isinstance(good, HuntProbe) and good.fault(context) == ""
    assert "is not a line" in probe(
        probe="hunt", pattern="IMPL-1", fact="the probe kinds are listed by hand twice",
        at="src/agent_comms/no_such_module.py:3", derive="declaration", by=at).fault(context)


def test_a_flinch_opens_yes_or_no_and_cites_evidence(context):
    assert probe(probe="flinch", answer="Yes. I called eager tasks a tradeoff before reading"
                                        " src/agent_comms/projects/rule_probes.py:1").fault(context) == ""
    assert probe(probe="flinch", answer="Yes, message 7525ef822fc9 was where").fault(context) == ""
    assert "yes or no" in probe(probe="flinch", answer="I resisted nothing").fault(context)
    assert "cites no" in probe(probe="flinch", answer="No, nothing to cite").fault(context)


def test_an_answer_mostly_copied_from_a_rule_is_refused(context):
    copied = probe(probe="flinch", answer="No: a rule says reach for a parent class before a flag"
                                          " 7525ef822fc9")
    assert "repeats the rules" in copied.fault(context)


def test_a_deal_with_no_words_of_his_owes_no_prior():
    assert PriorProbe not in Probe.owed(())
    assert set(Probe.owed(HIS)) == {PriorProbe, HuntProbe, FlinchProbe}
    assert Probe.missing((), HIS) == Probe.owed(HIS)


def test_a_hunt_cites_a_line_in_a_registered_checkout_outside_the_launch_directory(tmp_path):
    """Where a session was started is not where the team's code is.

    On 2026-11-07 a session launched in a course project owed the hunt probe, every line it
    was about to edit lived in a Toad worktree, and the gate read only the launch directory:
    the probe could not be answered at all, and the tools that could have found a line in
    that directory were the ones the gate was holding.
    """
    launched = tmp_path / "course-project"
    checkout = tmp_path / "toad"
    (launched / "notes").mkdir(parents=True)
    (checkout / "src").mkdir(parents=True)
    (checkout / "src" / "frame_presentation.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    repos = (Repo(checkout=checkout, github=RepositoryName.from_wire("OpenHCSDev/toad")),)
    context = ProbeContext(HIS, launched, repos, ("a rule about parents",))

    cited = "src/frame_presentation.py:2"
    hunt = probe(probe="hunt", pattern="IDEN-3", fact="the rows not risen yet, counted twice",
                 at=cited, derive="declaration", by=cited)
    assert hunt.fault(context) == ""

    # The refusal names every checkout it read, because the session cannot list them itself.
    fault = probe(probe="hunt", pattern="IDEN-3", fact="the rows not risen yet, counted twice",
                  at="src/frame_presentation.py:99", derive="declaration", by=cited).fault(context)
    assert str(launched) in fault and str(checkout) in fault


def test_a_bus_with_no_registered_project_reads_the_launch_directory_alone(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "owner.py").write_text("one\n", encoding="utf-8")
    context = ProbeContext(HIS, tmp_path, (), ("a rule about parents",))
    assert context.roots() == (tmp_path,)
    assert probe(probe="hunt", pattern="IDEN-3", fact="one fact restated in two owners",
                 at="src/owner.py:1", derive="declaration", by="src/owner.py:1").fault(context) == ""


def test_the_deal_prints_the_shape_of_every_probe_it_demands():
    """A session cannot read this module until its audit parses, so one that has not seen
    these shapes could only learn them from refusals, and the two type errors each give away
    half the answer: a tagged object is told the discriminator is wrong, a bare string is
    told it wants an object. On 2026-11-08 that cost four sessions an hour between them,
    while the same template had been printing MeantAnswer's declaration-derived shape all
    along. Derived from the family, so a new probe member is documented by existing.
    """
    due = AuditDue(owner_words=HIS)
    printed = due.template(Path("/tmp/session.audit.json"))

    assert Probe.family_discriminator in printed, "nothing says which key names the member"
    owed = Probe.owed(HIS)
    assert owed, "a deal that owes no probe cannot show their shapes"
    for member in owed:
        assert member.declared_name in printed
        assert member.question in printed
        for required in FieldCodec.record_schema(member)["required"]:
            assert required in printed, f"{member.declared_name} hides its {required} field"

    # Two facts nobody should have to discover from a refusal: which mechanisms `derive`
    # admits, and how a probe cites a file in another registered repository.
    assert "base-method" in printed
    assert "owner/name:" in printed


def test_every_failing_probe_is_named_in_one_refusal():
    from types import SimpleNamespace

    from agent_comms.team.rule_deal import AuditDue as Due

    due = Due(owner_words=HIS)
    audit = SimpleNamespace(probes=(
        probe(probe="prior", words="use a flag", refuted_by="Corollary 4.12",
              prior="a boolean felt lighter than one more base in the list"),
        probe(probe="hunt", pattern="IMPL-12", fact="a boolean flag restates the base class",
              at="no/such/file.py:1", derive="base-method", by="no/such/file.py:2"),
        probe(probe="flinch", answer="Yes: the check changed the plan, see abc123def456"),
    ))
    refusal = due.probe_refusal(audit)
    assert "Tristan's recorded prompts" in refusal
    assert "no/such/file.py:1" in refusal
    assert refusal.count("probe does not answer") == 2
