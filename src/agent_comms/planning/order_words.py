from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import (
    ClassVar,
    Self,
)

from agent_comms.nominal.field_codec import FieldCodec, MalformedRecord, WireValue
from agent_comms_hooks import ToolRefusedError


@dataclass(frozen=True)
class OrderWords(WireValue):
    """Text the teammate writes; quoted, backticked, indented and path-like spans are someone else's words."""

    # A path/file match starts at a token boundary or immediately after a
    # quoted span. Retrying its greedy prefix at every character is quadratic
    # for long ordinary words with no slash or filename suffix.
    CITED: ClassVar[re.Pattern] = re.compile(
        r'(?m)^ {4}.*$|`[^`\n]*`|"[^"\n]*"|“[^”\n]*”|'
        r'(?:(?<!\S)|(?<=[`"”]))(?:\S*/\S*|\S+\.(?:py|md|jsonl?|txt|png|toml|ts|log|patch)\b)|'
        r'\b\w+_\w+\b|\b[A-Z][a-z0-9]+[A-Z]\w*\b')  # identifiers are code, not prose
    REFUSED: ClassVar[re.Pattern]
    refusal: ClassVar[str]
    least_words: ClassVar[int] = 1
    text: str

    def __post_init__(self) -> None:
        if (refused := self.wording_refusal(self.text)) is not None:
            raise ToolRefusedError(refused)

    def to_wire(self) -> str:
        return self.text

    @classmethod
    def from_wire(cls, data: object) -> Self:
        if not isinstance(data, str):
            raise MalformedRecord(f"Expected {cls.__name__}, received {type(data).__name__}")
        with MalformedRecord.refusing(f"Invalid {cls.__name__}"):
            return cls(data)

    @classmethod
    def schema(cls) -> dict[str, object]:
        return FieldCodec.value_schema(str)

    @classmethod
    def refused(cls, text: str) -> str | None:
        """The writer's check: it may print a note, which a decoded record never does."""
        cls.flag(text)
        return cls.wording_refusal(text)

    @classmethod
    def wording_refusal(cls, text: str) -> str | None:
        """The refusal this text earns, or none: what a decoded record checks, silently."""
        found = cls.found(text)
        if len(text.split()) < cls.least_words or cls.refuses(found):
            return cls.refusal.format(cls.least_words, found)
        return None

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return bool(found)

    @classmethod
    def flag(cls, text: str) -> None:
        """A family that refuses has no note to print; one that only flags overrides this."""

    @classmethod
    def found(cls, text: str) -> list[str]:
        return sorted({m.group(0).lower() for m in cls.REFUSED.finditer(cls.CITED.sub(' ', text))})


class DirectiveWords(OrderWords):
    """A wording check every text sent to units passes; each check declares itself one."""


class PlainText(DirectiveWords):
    """Prose in the words the user uses: no word that hides what happened."""

    REFUSED = re.compile(r'\b(custody|admission|admit\w*|receipt\w*|checkpoint\w*|provenance|authority|authorities|witness\w*|lattice|surface|disposition\w*|invariant\w*|semantic\w*|ownership|lifecycle|acceptance|canonical|retained|borrow(?:ed|er|ers)?|bounded|certif\w*|recertif\w*|candidate|fence\w*|sidecar\w*|substrate|installed-path|source[- ]paired|exact[- ]head|integration owner|separately owned|follow-?up|remaining scope|not claimed|remains held|load[- ]bearing|cutover|carr(?:y|ies) across|declared dependency|qualifying)\b', re.I)
    refusal = "Jargon {1} hides what happened; say it plainly."

    @staticmethod
    def refuses(found: list[str]) -> bool:
        """Flags, never blocks: a wording slip costs a note, not a resend."""
        return False

    @classmethod
    def flag(cls, text: str) -> None:
        """The note goes to whoever writes the text, at the write; decoding a record never prints it."""
        if found := cls.found(text):
            print(f"Flag: jargon {', '.join(found)} hides what happened; say it plainly.", file=sys.stderr)


class VerdictReason(PlainText):
    """Why a reviewer decided a head, in words that stand on the public record."""

    refusal = "Say why in {}+ words; jargon {} hides what happened."
    least_words = 6


class GrowthReason(PlainText):
    """Why each growing source file needs its new lines, naming every one of them."""

    refusal = "Say why the source grows in {}+ words; jargon {} hides what happened."
    least_words = 12
    option: ClassVar[str] = '--growth'

    @classmethod
    def every(cls, texts: tuple[str, ...]) -> GrowthReason:
        """One reason per repeated --growth, each held to the word minimum, on one record."""
        return cls("; ".join(cls(text).text for text in texts))

    def names(self, path: str) -> bool:
        """One whole word of the reason is the file's path or its file name."""
        words = {word.strip(".,;:()`'\"") for word in self.text.split()}
        return bool({path, Path(path).name} & words)


class OrderCorrectionReason(PlainText):
    """Why the recorded part of an order was wrong, in enough words to stand on the record."""

    refusal = (
        "Say what was wrong with the recorded order in {}+ words; jargon {} hides what happened."
    )
    least_words = 8


class StrikeReason(VerdictReason):
    """What a unit did, plainly, in enough words to stand on the public record. A unit's
    wording is answered by a correction, never a strike: a reason whose every quoted span
    is jargon is about wording alone."""

    refusal = "Say what the teammate did plainly in {}+ words; jargon {} hides what happened."
    least_words = 10

    @classmethod
    def wording_refusal(cls, text: str) -> str | None:
        cited = [span.group(0).strip('`””” ') for span in cls.CITED.finditer(text)]
        if cited and all(PlainText.found(span) for span in cited):
            return f”{cited} is the teammate's wording; send it a correction, not a strike.”
        return super().wording_refusal(text)


class OrderText(OrderWords):
    """An order states the edit; it never hands the decision back or asks permission."""

    REFUSED = re.compile(r"\b(please|could you|would you|can you|if possible|consider|maybe|perhaps|when you get a chance|"
                         r"feel free|if you (think|agree|want)|thoughts\??|what do you think|thanks|thank you|sorry|"
                         r"no rush|optional(ly)?|up to you|might want|suggest)\b", re.I)
    refusal = "Soft wording {1}: state the edit, do not ask."


class BluntWords:
    """The words that name witnessed incompetence, and the refusal a text lacking them gets."""

    WORDS: ClassVar[tuple[str, ...]] = (
        "stupid", "slop", "bullshit", "useless", "wrong", "lazy", "garbage", "nonsense", "incompetent", "incompetently",
        "incompetence", "sloppy", "theater", "dumb", "worthless", "broke", "broken", "pointless", "waste")
    REFUSED: ClassVar[re.Pattern] = re.compile(rf"\b({'|'.join(WORDS)})\b", re.I)
    refusal: ClassVar[str] = "Not blunt: name what was stupid/slop/wrong and why, or pass --clean if nothing was."


class BluntText(BluntWords, OrderWords):
    """Witnessed incompetence is named as such: the text must call something stupid, slop or wrong."""

    @staticmethod
    def refuses(found: list[str]) -> bool:
        return not found

    @classmethod
    def wording_refusal(cls, text: str) -> str | None:
        """The refusal names every word that passes, so the next try is the last."""
        refusal = super().wording_refusal(text)
        if refusal is None:
            return None
        return f"{refusal} Any one of these words passes: {', '.join(cls.WORDS)}."


class DeliveryReport(BluntText):
    """A unit's delivery names the slop it killed or the broken thing it fixed, in the owner's
    voice; a status memo with no verdict in it is half a report."""

    refusal: ClassVar[str] = (
        "Flat report refused: name the slop you killed or the broken thing you fixed, with "
        "file:line, in the owner's voice (blunt, colorful, deadpan, and funny: land one joke on the code). A report with no opinion "
        "in it is half a report."
    )


class FairText(DirectiveWords):
    """The OOPSLA paper proves multiple parents derive every required pair with nothing outside, which composition and
    forwarding supply outside (Fig 3, Thm 4.6): faulting inheritance itself is dogma, not a restated fact.
    Only explanatory prose is checked; code and banned-action fields are not prose."""

    REFUSED = re.compile(r"composition over inheritance|(prefer|favou?r|use) composition|inherits (two|both|multiple|several|\d+)\b"
                         r"|multiple (inheritance|parents) (is (bad|wrong|dangerous|confusing|fragile|slop)|causes|creates|smells|complicates)"
                         r"|diamond problem|fragile base|(avoid|no|stop using|drop) (inheritance|mixins?|multiple (parents|inheritance))"
                         r"|too many (parents|base classes|mixins)|deep (inheritance|hierarch)|mixing (of )?\w+ and \w+"
                         r"|inheritance (abuse|soup|hell|is wrong)", re.I)
    refusal = ("{1} faults inheritance itself. Cite an NRA pattern ID, "
               "name the restated fact and the declaration that derives it.")


class HierarchyText(DirectiveWords):
    """A forwarder restates an implementation pair: explanatory prose must fix the
    hierarchy rather than preserve a forwarding route."""

    REFUSED = re.compile(r"(leave|keep|retain|preserve)[^.\n]{0,60}\bforward|forwarders? (stay|remain)|reach(es|ing)? through", re.I)
    refusal = ("{1} keeps a forwarder or bans fixing the hierarchy: fold the false owner into the real one or compose "
               "providers as parents, and delete every route.")


class DirectiveText:
    """What the teammate and the owner's voice send units: order send, order say and the
    connector's send refuse a text failing any of these."""

    @staticmethod
    def checks() -> tuple[type[DirectiveWords], ...]:
        return tuple(DirectiveWords.__subclasses__())

    @classmethod
    def refused(cls, text: str) -> tuple[str, ...]:
        return tuple(refused for family in cls.checks() if (refused := family.refused(text)))

    @classmethod
    def require(cls, text: str) -> None:
        if refused := cls.refused(text):
            raise ToolRefusedError("Directive refused, nothing sent: " + " ".join(refused))
