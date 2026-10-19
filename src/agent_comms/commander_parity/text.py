"""The native field codec and current commander's real wording refusals."""

from ..parity_checks import TextParityCheck, TextSample
from ..plan_phases import RecordPlanePhase
from ..todos import OrderText, PlainText, StrikeReason


class PlainWordsParityCheck(TextParityCheck):
    number = 9
    capability = "Plain words in verdicts and strikes"
    phases = (RecordPlanePhase,)
    samples = (
        TextSample(PlainText, "The file now reads the order from its original row.", True),
        TextSample(PlainText, "The receipt certifies the authority.", False),
        TextSample(PlainText, "Remove the quoted `authority` from that message.", True),
        TextSample(StrikeReason, "The unit changed another file and left the ordered repair broken.", True),
        TextSample(StrikeReason, "The edit broke.", False),
    )


class OrderWordingParityCheck(TextParityCheck):
    number = 10
    capability = "Refuse soft or vague orders"
    phases = (RecordPlanePhase,)
    samples = (
        TextSample(OrderText, "Repair the existing reader and remove the duplicate store.", True),
        TextSample(OrderText, "Could you repair the existing reader when you get a chance?", False),
        TextSample(OrderText, "Consider keeping both stores if possible.", False),
        TextSample(OrderText, "Remove the literal `please` from the help message now.", True),
    )


class UnitWordingParityCheck(TextParityCheck):
    number = 25
    capability = "Unit wording gets a correction, not a strike"
    phases = (RecordPlanePhase,)
    samples = (
        TextSample(StrikeReason,
                   "The unit wrote `canonical` instead of explaining the file that actually failed.",
                   False),
        TextSample(StrikeReason,
                   "The unit edited `/project/reader.py` but left the requested repair completely broken.",
                   True),
    )
