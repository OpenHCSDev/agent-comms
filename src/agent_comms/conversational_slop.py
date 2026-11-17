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
    ForbiddenPhraseVerifier,
    LengthRunawayVerifier,
    MissingCertaintyBlockVerifier,
    TerminalRepetitionVerifier,
)


def verify_all(response: str, context: ResponseContext) -> SlopVerdict:
    """Run every verifier and return the aggregate verdict."""
    results = {v.declared_name: v.check(response, context) for v in VERIFIERS}
    return SlopVerdict(results=results)
