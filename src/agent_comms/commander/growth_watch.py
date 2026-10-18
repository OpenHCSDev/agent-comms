"""Every merge to a fleet repository's main that grows src by more than LIMIT net lines prints a GROWTH alarm.

Approvals per edit let large net growth slip through; each merge is judged once, by its own
first-parent diff, at the moment it lands.
"""
import json, subprocess, time
from pathlib import Path

from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
SEEN = CONFIG.state / 'growth_seen.json'
LIMIT = 40


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True).stdout


def net(repo, sha, path):
    added = deleted = 0
    for line in git(repo, 'diff', '--numstat', f'{sha}^1', sha, '--', path).splitlines():
        a, d, _ = line.split('\t', 2)
        if a != '-':
            added, deleted = added + int(a), deleted + int(d)
    return added, deleted


def main(a):
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    first = not seen
    while True:
        for repo in CONFIG.repos:
            git(repo.checkout, 'fetch', '-q', 'origin', 'main')
            for line in git(repo.checkout, 'log', '--first-parent', '--since=12 hours ago', '--format=%H %s', 'origin/main').splitlines():
                sha, subject = line.split(' ', 1)
                if sha in seen:
                    continue
                a, d = net(repo.checkout, sha, repo.source)
                ta, td = net(repo.checkout, sha, 'tests')
                seen[sha] = [repo.github, a, d, ta, td]
                if not first and (a - d > LIMIT or (ta - td > 0 and ta - td > (a - d))):
                    print(f'GROWTH {repo.github} {sha[:9]} src +{a}/-{d} (net {a-d:+}) tests +{ta}/-{td}: {subject[:90]} '
                          f'-> justify or send back the owner with a deletion order', flush=True)
        SEEN.write_text(json.dumps(seen))
        first = False
        time.sleep(120)
