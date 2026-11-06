from pathlib import Path

import pytest

from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.rule_probes import FlinchProbe, HuntProbe, PriorProbe, Probe, ProbeContext

HIS = ("cam we use knheritsmce rather than a flag",)


@pytest.fixture
def context() -> ProbeContext:
    return ProbeContext(HIS, Path.cwd(), ("a rule says reach for a parent class before a flag",))


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
    at = "src/agent_comms/rule_probes.py:1"
    good = probe(probe="hunt", pattern="IMPL-1", fact="the probe kinds are listed by hand twice",
                 at=at, derive="declaration", by=at)
    assert isinstance(good, HuntProbe) and good.fault(context) == ""
    assert "is not a line" in probe(
        probe="hunt", pattern="IMPL-1", fact="the probe kinds are listed by hand twice",
        at="src/agent_comms/no_such_module.py:3", derive="declaration", by=at).fault(context)


def test_a_flinch_opens_yes_or_no_and_cites_evidence(context):
    assert probe(probe="flinch", answer="Yes. I called eager tasks a tradeoff before reading"
                                        " src/agent_comms/rule_probes.py:1").fault(context) == ""
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
