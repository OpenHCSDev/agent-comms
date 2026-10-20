"""Rule review for the commander: a report, never a gate.

The rules have one source each: the commander skill's bold-lead lines, every packaged rule file
in `rules/`, and paper 1's correct-maintenance results (the packaged correct-maintenance rule).
`review` prints every rule with its file:line and draws three, two always from the paper, to be
answered with how the current tick obeys or breaks each. Core records each review and answer
(CommsRuleReviewTool); a stale or unanswered review shows as RULES DUE in rotate and in
`rules stale`. No commander action is refused for it. The archived originals in `rules-archive/`
are never read.

  agent-comms-commander rules review [--fork FORK]                 print the rules, draw three
  agent-comms-commander rules answer N "<8+ quoted rule words and file:line or P-number>" [--fork FORK]

Every fork reviews for itself, and all three of a fork's drawn rules come from paper 1.
"""
import random, re, sys
from dataclasses import dataclass
from pathlib import Path


from ..field_codec import option
from ..rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviewEntry
from ..standing_rules import RulesDue
from ..tool_caller import ThreadCaller
from ..tools import CommsRuleReviewTool, StandingRulesRead
from .config import CommanderConfig
from .order import Commander, CommanderVerb
from .rule_file import RuleFile

PAPER = f'{CommanderConfig.packaged_rules()}/correct-maintenance.md'
DRAWN = 3
# Ingrained defaults these rules exist to break; shown and answered at every review, never left to the draw.
PINNED = ('slop-and-polymorphism',)


def principles() -> list[str]:
    """Paper 1's correct-maintenance results, the definitions every judgment is measured against."""
    text = RuleFile.read(Path(PAPER)).body
    return [l[2:].strip() for l in text.splitlines() if l.startswith('- ') and len(l) > 40]


def rules() -> list[tuple[str, str]]:
    """Every rule as (file:line, text)."""
    skill = CommanderConfig.packaged_skill()
    text = skill.read_text()
    found = [(f'{skill.name}:{text.count(chr(10), 0, m.start()) + 1}', (m.group(1) + m.group(2)).strip())
             for m in re.finditer(r'^\s*- \*\*(.+?)\*\*(.*)$', text, re.M)]
    for rule in RuleFile.packaged():
        found.extend((f'rules/{rule.name}.md:1', line) for line in rule.review_rules())
    return found


def record(ctx: Commander, who: str, entry: RuleReviewEntry) -> None:
    read: StandingRulesRead = ThreadCaller(who).invoke(CommsRuleReviewTool(entry=entry), ctx.comms)
    print('\n'.join(read.broken) or 'review complete')


def review(ctx: Commander, who: str) -> None:
    located = rules()
    for i, (where, r) in enumerate(located, 1):
        print(f'{i:3}. {where}  {r}')
    every = [r for _, r in located]
    paper = principles()
    print('\nCorrect maintenance (paper 1; every judgment is measured against these):')
    for r in paper:
        print(f'  - {r}')
    drawn = random.sample(paper, DRAWN) if who != ctx.commander else random.sample(paper, 2) + random.sample(every, DRAWN - 2)
    pinned = [r for r in every if r.split(':', 1)[0] in PINNED]
    if len(pinned) != len(PINNED):
        sys.exit(f'Pinned rule missing from the rule directories: {sorted(set(PINNED) - {r.split(":", 1)[0] for r in pinned})}')
    drawn += [r for r in pinned if r not in drawn]
    record(ctx, who, DrawnRuleReviewEntry(tuple(drawn)))
    print('\nAnswer each with what this tick does about it (agent-comms-commander rules answer N "..."):')
    for n, r in enumerate(drawn, 1):
        print(f'  {n}. {r}')


def answer(ctx: Commander, n: int, text: str, who: str) -> None:
    record(ctx, who, AnsweredRuleReviewEntry(n, text))


class RulesVerb(CommanderVerb):
    """Rules use the shared acting-commander declaration and gate."""


class ReviewVerb(RulesVerb):
    help = "print the standing rules and draw the required review"

    def run(self, ctx: Commander) -> None:
        review(ctx, self.who(ctx))


@dataclass(frozen=True, kw_only=True)
class AnswerVerb(RulesVerb):
    help = "answer a drawn rule for the acting commander"
    n: int = option("n", help="the drawn rule number")
    text: str = option("text", help="how this tick obeys or breaks that rule")

    def run(self, ctx: Commander) -> None:
        answer(ctx, self.n, self.text, self.who(ctx))


class StaleVerb(RulesVerb):
    help = "show whether the acting commander's rule review is due"

    def run(self, ctx: Commander) -> None:
        print('\n'.join(due.why for due in RulesDue.violations(ctx.comms, self.who(ctx)))
              or 'fresh')


def main(a, ctx):
    try:
        RulesVerb.parse(a, __doc__).apply(ctx)
    except ValueError as refused:  # Core owns what a review and an answer may say
        sys.exit(f'REFUSED: {refused}')
