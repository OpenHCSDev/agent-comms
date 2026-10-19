"""Forced rule review for the commander (HARD RULE: the commander cannot trust itself to remember the rules alone).

The rules have one source each: the commander skill's bold-lead lines, every feedback rule in the configured rule
directories (the packaged rules first, then the project's own memory), and paper 1's correct-maintenance results
(the packaged correct-maintenance rule). `review` prints all of them and draws three, two always from the paper, that must each be answered with how the current tick obeys or breaks it. Core records each
review and answer (CommsRuleReviewTool); its RulesDue member refuses every commander action in order and prints
RULES DUE in rotate until the last review is answered and fresh.

  agent-comms-commander rules review [--fork FORK]                 print the rules, draw three
  agent-comms-commander rules answer N "<10+ words>" [--fork FORK]  answer drawn rule N with what this tick does about it

Every fork reviews for itself (forks must know correct maintenance, not carry dogma): a fork's
actions are refused until its own review is answered, and all three of a fork's drawn rules come from paper 1.
"""
import glob, os, random, re, sys
from dataclasses import dataclass

from ..cli_commands import option

from ..rule_reviews import AnsweredRuleReviewEntry, DrawnRuleReviewEntry, RuleReviewEntry
from ..standing_rules import RulesDue
from ..tool_caller import ThreadCaller
from ..tools import CommsRuleReviewTool, StandingRulesRead
from .config import CommanderConfig
from .order import CommanderVerb

PAPER = f'{CommanderConfig.packaged_rules()}/correct-maintenance.md'
DRAWN = 3
# Ingrained defaults these rules exist to break; shown and answered at every review, never left to the draw.
PINNED = ('branch-vs-polymorphism', 'factor-into-parent-hard-rule', 'paper-is-law-no-dogma')


def principles() -> list[str]:
    """Paper 1's correct-maintenance results, the definitions every judgment is measured against."""
    text = open(PAPER).read()
    return [l[2:].strip() for l in text.splitlines() if l.startswith('- ') and len(l) > 40]


def rules(commander) -> list[str]:
    found = [m.group(1).strip() for m in re.finditer(r'^\s*- \*\*(.+?)\*\*', open(str(commander.skill)).read(), re.M)]
    for path in [path for directory in commander.rules for path in sorted(glob.glob(f'{directory}/*.md'))]:
        text = open(path).read()
        if re.search(r'^\s*type: feedback', text, re.M):
            desc = re.search(r'^description: (.+)$', text, re.M)
            found.append(f'{os.path.basename(path)[:-3]}: {desc.group(1) if desc else ""}')
    return found


def record(commander, who: str, entry: RuleReviewEntry) -> None:
    read: StandingRulesRead = ThreadCaller(who).invoke(CommsRuleReviewTool(entry=entry), commander.comms)
    print('\n'.join(read.broken) or 'review complete; commander actions allowed')


def review(commander, who: str):
    every = rules(commander)
    for i, r in enumerate(every, 1):
        print(f'{i:3}. {r}')
    paper = principles()
    print('\nCorrect maintenance (paper 1; every judgment is measured against these):')
    for r in paper:
        print(f'  - {r}')
    drawn = random.sample(paper, DRAWN) if who != commander.commander else random.sample(paper, 2) + random.sample(every, DRAWN - 2)
    pinned = [r for r in every if r.split(':', 1)[0] in PINNED]
    if len(pinned) != len(PINNED):
        sys.exit(f'Pinned rule missing from the rule directories: {sorted(set(PINNED) - {r.split(":", 1)[0] for r in pinned})}')
    drawn += [r for r in pinned if r not in drawn]
    record(commander, who, DrawnRuleReviewEntry(tuple(drawn), len(drawn) if who != commander.commander else 2))
    print('\nAnswer each with what this tick does about it (agent-comms-commander rules answer N "..."):')
    for n, r in enumerate(drawn, 1):
        print(f'  {n}. {r}')


def answer(commander, n: int, text: str, who: str):
    record(commander, who, AnsweredRuleReviewEntry(n, text))


class RulesVerb(CommanderVerb):
    """Rules use the shared acting-commander declaration and gate."""


class ReviewVerb(RulesVerb):
    help = "print the standing rules and draw the required review"

    def run(self) -> None:
        review(self.context, self.who)


@dataclass(frozen=True, kw_only=True)
class AnswerVerb(RulesVerb):
    help = "answer a drawn rule for the acting commander"
    n: int = option("n", help="the drawn rule number")
    text: str = option("text", help="how this tick obeys or breaks that rule")

    def run(self) -> None:
        answer(self.context, self.n, self.text, self.who)


class StaleVerb(RulesVerb):
    help = "show whether the acting commander's rule review is due"

    def run(self) -> None:
        print('\n'.join(due.why for due in RulesDue.violations(self.context.comms, self.who)) or 'fresh')


def main(a, commander):
    try:
        RulesVerb.parse(a, __doc__, commander).apply()
    except ValueError as refused:  # Core owns what a review and an answer may say
        sys.exit(f'REFUSED: {refused}')
