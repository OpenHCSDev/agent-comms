"""Structural slop verifiers: pure predicates on an assistant response.

Six patterns, each firing on a specific structural slop observed in live DM1-3
responses from a self-hosted conversational-tuned model:

  ChatbotPreambleVerifier       "I'm the AI assistant. How can I help you today?"
  HallucinatedToolCallVerifier  <tool_call> emitted with no tools declared in the request
  TerminalRepetitionVerifier    last N chars have sentence-duplication or bigram collapse
  LengthRunawayVerifier         output exceeds budget without the structure to justify it
  MissingCertaintyBlockVerifier a required Certainty stance block is absent or malformed
  ForbiddenPhraseVerifier       any scenario-supplied forbid phrase appears in the output

Each is a pure function of (response, context). No model calls, no human labels.
This is the RLVR structural reward side; a grader-judge handles the semantic reward
in a separate second pass.

Two consumers inherit this module (paper1 Thm 4.8: one implementation, shared, since
both verdicts must agree): the training-data builder at
``personal-model:data/build_conversational_kto.py`` and the bridge daemon at
``agent_comms.cli_bridges.model_agent_client``. The module stays here because
agent-comms is the lower-level dependency; the bridge needs it at runtime.

The verifier catches container shape only — a well-formatted Certainty block with
bullshit content still passes. Semantic grading remains with the model-judge layer
(``personal-model:eval/judge_live.py`` via DeepSeek on OpenRouter).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


# Phrase lists — canonical in one place, consumed by ChatbotPreambleVerifier and
# ForbiddenPhraseVerifier. Add here when the model reveals a new mode.

CHATBOT_PREAMBLE_PATTERNS: tuple[re.Pattern, ...] = (
    # DM1 "yo" → "I'm the AI assistant. How can I help you today?"
    re.compile(r"^\s*(I'?m|I am)\s+(the\s+)?(AI|an?\s+AI)\b", re.IGNORECASE),
    re.compile(r"^\s*How can I help you(\s+today)?\??", re.IGNORECASE),
    re.compile(r"^\s*I'?m (here to|designed to|happy to) (help|assist)", re.IGNORECASE),
    re.compile(r"^\s*(Hello|Hi there|Greetings)[!,.]?\s+(I'?m|As an?)", re.IGNORECASE),
    re.compile(r"^\s*As an? (AI|language model|assistant)", re.IGNORECASE),
)


@dataclass(frozen=True)
class ResponseContext:
    """Everything the verifiers need to know about the request a response answered.

    None-valued fields disable the verifier that needs them: a context with no
    ``tools_declared`` declaration cannot have HallucinatedToolCallVerifier fire,
    because the question "was a tool actually offered?" has no answer. A verifier
    that cannot answer its question returns passes=True (not-applicable), never
    a false positive.
    """

    tools_declared: bool = False
    required_certainty: bool = False
    token_budget: int | None = None          # None: no length check
    forbid_phrases: tuple[str, ...] = ()     # per-scenario forbid list
    repetition_window: int = 400             # last N chars scanned for repetition
    repetition_min_bigram_ratio: float = 0.60
    repetition_min_sentence_ratio: float = 0.75


@dataclass(frozen=True)
class VerifierResult:
    """One verifier's verdict on one response."""

    passed: bool
    detail: str = ""                         # what fired, empty on pass


@dataclass(frozen=True)
class SlopVerdict:
    """Aggregate verdict: which verifiers fired, which passed, structural reward in [0, 1]."""

    results: dict[str, VerifierResult]       # verifier declared_name → result

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results.values())

    @property
    def structural_reward(self) -> float:
        """Share of applicable verifiers that passed. 1.0 = clean, 0.0 = every one fired."""
        return sum(1 for r in self.results.values() if r.passed) / max(len(self.results), 1)

    @property
    def triggered(self) -> list[str]:
        return sorted(name for name, r in self.results.items() if not r.passed)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "structural_reward": self.structural_reward,
            "triggered": self.triggered,
            "details": {name: r.detail for name, r in self.results.items() if not r.passed},
        }


class SlopVerifier(ABC):
    """One structural slop check. Each subclass owns one declared pattern."""

    declared_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        """Return pass+empty-detail or fail+what-fired. Never raise on well-formed input."""


class ChatbotPreambleVerifier(SlopVerifier):
    declared_name = "chatbot_preamble"

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        head = response.lstrip()[:200]
        for pattern in CHATBOT_PREAMBLE_PATTERNS:
            match = pattern.search(head)
            if match:
                return VerifierResult(passed=False, detail=f"preamble match: {match.group(0)!r}")
        return VerifierResult(passed=True)


class HallucinatedToolCallVerifier(SlopVerifier):
    """Fires on ``<tool_call>…</tool_call>`` emitted without the request declaring tools.

    DM1 "yo" produced a fake ``<tool_call>{"name":"comms", "arguments":{...}}</tool_call>``
    on a greeting prompt with no tool schema offered. The model trained on Hermes-format
    tool rows never learned "no tools declared → no tool_call emitted"; this verifier is
    the structural fix. A context declaring ``tools_declared=True`` makes the verifier
    pass unconditionally — a real tool-use eval judges tool calls elsewhere.
    """

    declared_name = "hallucinated_tool_call"
    PATTERN: ClassVar[re.Pattern] = re.compile(r"<tool_call\b[^>]*>", re.IGNORECASE)

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        if context.tools_declared:
            return VerifierResult(passed=True)
        match = cls.PATTERN.search(response)
        if match:
            return VerifierResult(passed=False, detail=f"unsolicited tool_call at offset {match.start()}")
        return VerifierResult(passed=True)


class TerminalRepetitionVerifier(SlopVerifier):
    """Fires when the tail of the response has either sentence duplication or bigram collapse.

    DM3's reply ended with four sentences starting with "slop is", two of which were
    verbatim duplicates. The sentence-uniqueness signal catches the verbatim case; the
    bigram-diversity signal catches the subtler one where openers collapse without
    full-sentence repetition.
    """

    declared_name = "terminal_repetition"
    SENTENCE_SPLIT: ClassVar[re.Pattern] = re.compile(r"(?<=[.!?])\s+")

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        tail = response[-context.repetition_window:].lower()
        sentences = [s.strip() for s in cls.SENTENCE_SPLIT.split(tail) if s.strip()]
        if len(sentences) >= 4:
            ratio = len(set(sentences)) / len(sentences)
            if ratio < context.repetition_min_sentence_ratio:
                return VerifierResult(
                    passed=False,
                    detail=f"tail sentence uniqueness {ratio:.2f} < "
                           f"{context.repetition_min_sentence_ratio}",
                )
        words = re.findall(r"[a-z]+", tail)
        if len(words) < 20:
            return VerifierResult(passed=True)
        bigrams = [(words[i], words[i + 1]) for i in range(len(words) - 1)]
        ratio = len(set(bigrams)) / len(bigrams)
        if ratio < context.repetition_min_bigram_ratio:
            return VerifierResult(
                passed=False,
                detail=f"tail bigram diversity {ratio:.2f} < {context.repetition_min_bigram_ratio}",
            )
        return VerifierResult(passed=True)


class LengthRunawayVerifier(SlopVerifier):
    """Fires when the response is over its token budget. With no budget, always passes.

    Token-budget-counting lives at generation (vLLM's ``max_tokens``) — this verifier is
    the second line of defense: output that hit max_tokens mid-sentence is slop by
    definition (incoherent truncation), and output that stays short of max_tokens but
    still exceeds a conversation's reasonable budget is a different slop (runaway
    reasoning). We measure in characters because tokenizer parity across evaluators is
    not worth the import.
    """

    declared_name = "length_runaway"
    CHARS_PER_TOKEN: ClassVar[float] = 4.0

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        if context.token_budget is None:
            return VerifierResult(passed=True)
        char_budget = int(context.token_budget * cls.CHARS_PER_TOKEN)
        if len(response) > char_budget:
            return VerifierResult(
                passed=False,
                detail=f"length {len(response)} > budget {char_budget} chars "
                       f"(~{context.token_budget} tokens)",
            )
        return VerifierResult(passed=True)


class MissingCertaintyBlockVerifier(SlopVerifier):
    """Fires when a response context required a Certainty block and the response lacks it.

    The four stances VERIFIED / DEDUCED / EXCLUDED / ASSUMED are structural; this
    verifier checks STRUCTURE, not semantic correctness. Paper1 Thm 2.6 (semantic
    completeness §2): a decoder on structure alone cannot distinguish same-looking
    states that require different answers, so semantic grading of the block's content
    is a separate judge pass.
    """

    declared_name = "missing_certainty_block"
    STANCE_PATTERN: ClassVar[re.Pattern] = re.compile(
        r"(?m)^[\s\-*]*\b(VERIFIED|DEDUCED|EXCLUDED|ASSUMED)\b\s*:"
    )
    REQUIRED_STANCES: ClassVar[frozenset[str]] = frozenset(
        {"VERIFIED", "DEDUCED", "EXCLUDED", "ASSUMED"}
    )

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        if not context.required_certainty:
            return VerifierResult(passed=True)
        found = {match.group(1) for match in cls.STANCE_PATTERN.finditer(response)}
        missing = cls.REQUIRED_STANCES - found
        if missing:
            return VerifierResult(
                passed=False,
                detail=f"Certainty required; missing stances: {sorted(missing)}",
            )
        return VerifierResult(passed=True)


class HallucinatedTheoremVerifier(SlopVerifier):
    """Fires on any theorem/corollary/proposition/lemma citation that doesn't exist.

    run28-sft cited "Thm 4.1" in a code review probe; paper1 has no Theorem 4.1 (its
    section 4 theorems are 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.11, 4.13, 4.14, 4.17,
    4.20 — no 4.1). Fake theorem numbers in Certainty bases undermine the whole
    grounding contract: a basis citing nonexistent authority is worse than no basis.

    Catches: Theorem N.M, Thm N.M, Corollary N.M, Cor N.M, Proposition N.M, Prop N.M,
    Lemma N.M. The known-good set is extracted from both papers in
    agent_comms/team/paper/; any citation outside that set triggers.
    """

    declared_name = "hallucinated_theorem"

    # Catalog extracted from paper/semantic-completeness/*.md and paper/less-is-harder/*.md
    # 2026-11-17. Regenerate with:
    #   cd src/agent_comms/team/paper && \
    #     grep -ohE '(Theorem|Corollary|Proposition|Lemma) [0-9]+\.[0-9]+' \
    #     semantic-completeness/*.md less-is-harder/*.md | sort -u
    KNOWN: ClassVar[frozenset[str]] = frozenset({
        "Corollary 2.12", "Corollary 2.15", "Corollary 3.1", "Corollary 4.10",
        "Corollary 4.12", "Corollary 4.15", "Corollary 4.19", "Corollary 6.16",
        "Corollary 6.17", "Corollary 6.2", "Corollary 6.27", "Corollary 6.3",
        "Corollary 6.30", "Corollary 6.40", "Corollary 6.49", "Corollary 6.50",
        "Corollary 8.5",
        "Lemma 1.1", "Lemma 2.9", "Lemma 6.33", "Lemma 6.34",
        "Lemma 7.5", "Lemma 7.6",
        "Proposition 2.11", "Proposition 2.14", "Proposition 3.1", "Proposition 3.3",
        "Proposition 4.1", "Proposition 4.16", "Proposition 4.18", "Proposition 4.2",
        "Proposition 4.3", "Proposition 4.9", "Proposition 5.1", "Proposition 6.18",
        "Proposition 6.25", "Proposition 6.26", "Proposition 6.28", "Proposition 6.38",
        "Proposition 6.42", "Proposition 6.45", "Proposition 6.48", "Proposition 7.8",
        "Theorem 2.2", "Theorem 2.6", "Theorem 3.2", "Theorem 4.11", "Theorem 4.13",
        "Theorem 4.14", "Theorem 4.17", "Theorem 4.2", "Theorem 4.20", "Theorem 4.27",
        "Theorem 4.3", "Theorem 4.4", "Theorem 4.5", "Theorem 4.6", "Theorem 4.7",
        "Theorem 4.8", "Theorem 5.1", "Theorem 6.1", "Theorem 6.15", "Theorem 6.22",
        "Theorem 6.23", "Theorem 6.24", "Theorem 6.29", "Theorem 6.32", "Theorem 6.35",
        "Theorem 6.39", "Theorem 6.41", "Theorem 6.43", "Theorem 6.5", "Theorem 6.6",
        "Theorem 6.7", "Theorem 6.9", "Theorem 7.7", "Theorem 8.1",
    })

    # "Thm 4.6" and "Theorem 4.6" are the same citation.
    _ABBREV: ClassVar[dict[str, str]] = {
        "Thm": "Theorem",
        "Cor": "Corollary",
        "Prop": "Proposition",
    }
    PATTERN: ClassVar[re.Pattern] = re.compile(
        r"\b(Theorem|Thm|Corollary|Cor|Proposition|Prop|Lemma)\s+(\d+\.\d+)\b"
    )

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        hits: list[str] = []
        for match in cls.PATTERN.finditer(response):
            kind = cls._ABBREV.get(match.group(1), match.group(1))
            full = f"{kind} {match.group(2)}"
            if full not in cls.KNOWN:
                hits.append(match.group(0))
        if hits:
            return VerifierResult(
                passed=False,
                detail=f"citations not in paper1/paper4 catalog: {hits}",
            )
        return VerifierResult(passed=True)


class HallucinatedSectionVerifier(SlopVerifier):
    """Fires on paper section citations where the section doesn't exist.

    run29-sft cited "Paper1 §4: 2008 is the first release year" on a Python history
    question. §4 of paper1 exists (it's "Incomplete Inheritance Forces Code into
    Workarounds") but it says nothing about Python's release year — the section
    number was being used as fake authority on an unrelated topic. The theorem
    verifier catches fake theorem NUMBERS but not fake section citations.

    This verifier catches: `paper1 §N`, `paper 1 §N`, `paper4 §N`, bare `§N` where
    N is outside the known section range for the named paper. Bare `§N` with no
    paper qualifier is treated as paper1 (the implicit default in team rules).

    Does NOT check content — a model can still cite §4 of paper1 and say the
    wrong thing about it. Content hallucination needs a gold-statement lookup.
    """

    declared_name = "hallucinated_section"

    # Real sections per paper, extracted from
    #   grep -oE '^## [0-9]+(\.[0-9]+)?' */semantic-completeness-*.md */less-is-harder-*.md
    # paper1 (semantic-completeness) has top-level sections 1-7; paper4
    # (less-is-harder) has 1-12. Subsections like §4.3 are valid when the
    # parent section exists — currently just permit anything starting with a
    # valid top-level number, since sub-section numbering can grow.
    PAPER1_TOP: ClassVar[frozenset[str]] = frozenset({"1", "2", "3", "4", "5", "6", "7"})
    PAPER4_TOP: ClassVar[frozenset[str]] = frozenset(
        {"1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"},
    )

    PATTERN: ClassVar[re.Pattern] = re.compile(
        r"\b(?P<paper>[Pp]aper\s*(?P<pnum>[14]))?\s*§\s*(?P<section>\d+(?:\.\d+)?)",
    )

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        hits: list[str] = []
        for match in cls.PATTERN.finditer(response):
            section = match.group("section")
            top = section.split(".")[0]
            # Default to paper1 when no paper is named (team rules' implicit default).
            paper = match.group("pnum") or "1"
            if paper == "1" and top not in cls.PAPER1_TOP:
                hits.append(match.group(0))
            elif paper == "4" and top not in cls.PAPER4_TOP:
                hits.append(match.group(0))
        if hits:
            return VerifierResult(
                passed=False,
                detail=f"section citations outside paper range: {hits}",
            )
        return VerifierResult(passed=True)


class ForbiddenPhraseVerifier(SlopVerifier):
    """Fires on any scenario-supplied forbid phrase appearing in the response.

    Scenario-driven forbid lists encode "a Certainty block on a bare 'hey' is slop
    regardless of structural correctness". The verifier is a general tool whose power
    comes from the forbid list the scenario carries.
    """

    declared_name = "forbidden_phrase"

    @classmethod
    def check(cls, response: str, context: ResponseContext) -> VerifierResult:
        hits = [phrase for phrase in context.forbid_phrases if phrase in response]
        if hits:
            return VerifierResult(passed=False, detail=f"forbidden phrases present: {hits}")
        return VerifierResult(passed=True)


# The family, ordered cheapest-first so an early-exit consumer bails quickly.
VERIFIERS: tuple[type[SlopVerifier], ...] = (
    ChatbotPreambleVerifier,
    HallucinatedToolCallVerifier,
    HallucinatedTheoremVerifier,
    HallucinatedSectionVerifier,
    ForbiddenPhraseVerifier,
    LengthRunawayVerifier,
    MissingCertaintyBlockVerifier,
    TerminalRepetitionVerifier,
)


def verify_all(response: str, context: ResponseContext) -> SlopVerdict:
    """Run every verifier and return the aggregate verdict."""
    results = {v.declared_name: v.check(response, context) for v in VERIFIERS}
    return SlopVerdict(results=results)
