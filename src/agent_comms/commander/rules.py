"""Forced rule review for the commander (HARD RULE: the commander cannot trust itself to remember the rules alone).

The rules have one source each: the commander skill's bold-lead lines, every feedback rule in the configured rule
directories (the packaged rules first, then the project's own memory), and paper 1's correct-maintenance results
(the packaged correct-maintenance rule). `review` prints all of them and draws three, two always from the paper, that must each be answered with how the current tick obeys or breaks it. order refuses every
commander action (send, strike, done, close, problem, hunt, say, fold, drop) until the last review is answered and
younger than MAX_AGE; rotate prints RULES DUE when it is stale.

  agent-comms-commander rules review [--as FORK]                 print the rules, draw three
  agent-comms-commander rules answer N "<10+ words>" [--as FORK]  answer drawn rule N with what this tick does about it

Every fork reviews for itself (forks must know correct maintenance, not carry dogma): a fork's
actions are refused until its own review is answered, and all three of a fork's drawn rules come from paper 1.
"""
import glob, json, os, random, re, sys, time

from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
ME = CONFIG.commander
LOG = f'{CONFIG.state}/rules.jsonl'
SKILL = str(CONFIG.skill)
PAPER = f'{CommanderConfig.packaged_rules()}/correct-maintenance.md'
MAX_AGE = 20 * 60
DRAWN = 3
# Ingrained defaults these rules exist to break; shown and answered at every review, never left to the draw.
PINNED = ('branch-vs-polymorphism', 'factor-into-parent-hard-rule', 'paper-is-law-no-dogma')


def principles() -> list[str]:
    """Paper 1's correct-maintenance results, the definitions every judgment is measured against."""
    text = open(PAPER).read()
    return [l[2:].strip() for l in text.splitlines() if l.startswith('- ') and len(l) > 40]


def rules() -> list[str]:
    found = [m.group(1).strip() for m in re.finditer(r'^\s*- \*\*(.+?)\*\*', open(SKILL).read(), re.M)]
    for path in [path for directory in CONFIG.rules for path in sorted(glob.glob(f'{directory}/*.md'))]:
        text = open(path).read()
        if re.search(r'^\s*type: feedback', text, re.M):
            desc = re.search(r'^description: (.+)$', text, re.M)
            found.append(f'{os.path.basename(path)[:-3]}: {desc.group(1) if desc else ""}')
    return found


def last(who: str = ME) -> dict | None:
    rows = [r for r in (json.loads(l) for l in open(LOG)) if r.get('who', ME) == who] if os.path.exists(LOG) else []
    return next((r for r in reversed(rows) if r['kind'] == 'review'), None), rows


def stale(who: str = ME) -> str | None:
    """Why this commander's actions are refused now, or None when its last review is answered and fresh."""
    review, rows = last(who)
    if review is None:
        return f'no rule review recorded for {who}'
    answered = {r['n'] for r in rows if r['kind'] == 'answer' and r['at'] >= review['at']}
    if missing := [n for n in range(1, len(review["drawn"]) + 1) if n not in answered]:
        return f'drawn rules {missing} of the {time.strftime("%H:%M", time.localtime(review["at"]))} review are unanswered'
    if time.time() - review['at'] > MAX_AGE:
        return f'last rule review was {int((time.time() - review["at"]) / 60)} min ago (max {MAX_AGE // 60})'
    return None


def stale_since(who: str = ME) -> float:
    """When the refusal began: the last review, or its expiry once it is older than MAX_AGE."""
    review, _ = last(who)
    if review is None:
        return 0.0
    return review['at'] + (MAX_AGE if time.time() - review['at'] > MAX_AGE else 0)


def require(who: str = ME):
    if why := stale(who):
        flag = '' if who == ME else f' --as {who}'
        sys.exit(f'REFUSED, nothing recorded: {why}. Run agent-comms-commander rules review{flag}, then answer each drawn rule{flag}.')


def review(who: str = ME):
    every = rules()
    for i, r in enumerate(every, 1):
        print(f'{i:3}. {r}')
    paper = principles()
    print('\nCorrect maintenance (paper 1; every judgment is measured against these):')
    for r in paper:
        print(f'  - {r}')
    drawn = random.sample(paper, DRAWN) if who != ME else random.sample(paper, 2) + random.sample(every, DRAWN - 2)
    pinned = [r for r in every if r.split(':', 1)[0] in PINNED]
    if len(pinned) != len(PINNED):
        sys.exit(f'Pinned rule missing from the rule directories: {sorted(set(PINNED) - {r.split(":", 1)[0] for r in pinned})}')
    drawn += [r for r in pinned if r not in drawn]
    with open(LOG, 'a') as f:
        f.write(json.dumps({'kind': 'review', 'who': who, 'at': time.time(), 'drawn': drawn}) + '\n')
    print('\nAnswer each with what this tick does about it (agent-comms-commander rules answer N "..."):')
    for n, r in enumerate(drawn, 1):
        print(f'  {n}. {r}')


def answer(n: int, text: str, who: str = ME):
    review, _ = last(who)
    if review is None or not 1 <= n <= len(review["drawn"]):
        sys.exit('REFUSED: no review drawn, or N out of range')
    if len(text.split()) < 10:
        sys.exit('REFUSED: say in 10+ words what this tick does about the rule, naming a unit, problem or file')
    if (n <= 2 or who != ME) and not re.search(r'\S+\.py:\d+|P\d+', text):
        sys.exit('REFUSED: a correct-maintenance answer names the file:line or P-number it was applied to')
    with open(LOG, 'a') as f:
        f.write(json.dumps({'kind': 'answer', 'who': who, 'at': time.time(), 'n': n, 'rule': review['drawn'][n - 1], 'text': text}) + '\n')
    print(stale(who) or 'review complete; commander actions allowed')


def main(a):
    who = ME
    if '--as' in a:
        i = a.index('--as'); who = a[i + 1]; del a[i:i + 2]
    if a == ['review']:
        review(who)
    elif a[:1] == ['answer'] and len(a) == 3 and a[1].isdigit():
        answer(int(a[1]), a[2], who)
    elif a == ['stale']:
        print(stale(who) or 'fresh')
    else:
        sys.exit(__doc__)
