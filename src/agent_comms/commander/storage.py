"""Storage rules for fleet units, one owner for every check: units never clean up after themselves.

Each unit owns at most MAX_WORKTREES checkouts named <worktrees>/<unit> or <worktrees>/<unit>--<suffix>, and one
scratch directory <scratch>/<unit>/. Tests run on the shared virtualenv of the <repo>-check checkout with
PYTHONPATH=src; no per-worktree venv, no .artifacts, no clone. Below FLOOR_GB free on the configured disk nothing
that allocates disk may run. order (send/done/overdue) and tool_watch both enforce through this module.
"""
import os, re, shlex, shutil, subprocess

from .config import CommanderConfig

CONFIG = CommanderConfig.installed()
WT = str(CONFIG.worktrees)
SCRATCH = str(CONFIG.scratch)
CLEANUP_GB = 30  # under this a cleanup owner is due; it does not stop builds
FLOOR_GB = 5  # under this nothing that allocates may run: a runtime is ~160 MB, a build's scratch ~1 GB
UNIT_CAP_GB = 3
MAX_WORKTREES = 2

RULE = (f'Storage (enforced, automatic strike): your checkouts are {WT}/<you>--P<n>, at most {MAX_WORKTREES} besides your home checkout {WT}/<you>, removed '
        f'with git worktree remove in the same order that created them; your only scratch dir is {SCRATCH}/<you>/, '
        f'capped at {UNIT_CAP_GB} GB together with your checkouts. Run tests with PYTHONPATH=src on the shared '
        f'{WT}/<repo>-check/.venv; never create a venv, never uv sync, never write .artifacts, never git clone. '
        f'Below {FLOOR_GB} GB free on {CONFIG.disk}, no worktree, venv, build, install or recording.')

ALLOCATES = re.compile(r'\bgit\s+worktree\s+add\b|\bgit\s+clone\b|\buv\s+(venv|sync)\b|-m\s+venv\b|\bvirtualenv\b|'
                       r'build_runtime\.py|\bpip\s+install\b|\buv\s+pip\s+install\b|py-spy\s+record|\bffmpeg\b|\brecordmydesktop\b')


def free_gb() -> float:
    return shutil.disk_usage(CONFIG.disk).free / 2**30


def owns(unit: str, name: str) -> bool:
    return name == unit or name.startswith(unit + '--')


def unit_paths(unit: str) -> tuple[list[str], list[str]]:
    trees = [f'{WT}/{n}' for n in os.listdir(WT) if owns(unit, n) and os.path.isdir(f'{WT}/{n}')]
    scratch = [f'{SCRATCH}/{n}' for n in os.listdir(SCRATCH) if owns(unit, n)]
    return trees, scratch


def usage_gb(unit: str) -> tuple[int, float]:
    trees, scratch = unit_paths(unit)
    paths = trees + scratch
    if not paths:
        return 0, 0.0
    out = subprocess.run(['du', '-sbc', *paths], capture_output=True, text=True).stdout.splitlines()
    total = int(out[-1].split()[0]) if out else 0
    return len(trees), total / 2**30


def over_quota(unit: str) -> str | None:
    trees, gb = usage_gb(unit)
    extra = [t for t in unit_paths(unit)[0] if os.path.basename(t) != unit]  # WT/<unit> is the worker's home checkout
    if len(extra) > MAX_WORKTREES:
        return f'{unit} holds {len(extra)} order checkouts in {WT} besides its home checkout (max {MAX_WORKTREES})'
    if gb > UNIT_CAP_GB:
        return f'{unit} uses {gb:.1f} GB of checkouts and scratch (cap {UNIT_CAP_GB} GB)'
    return None


NO_VALUE_FLAGS = r'(?:--detach|-d|--force|-f|--no-checkout|--checkout|--lock|-q|--quiet|--no-track|--track|--guess-remote|--orphan)'


def command_violations(unit: str, command: str) -> list[str]:
    """What this shell command does that the storage rules forbid, judged from its words."""
    found = []
    if re.search(r'\bgit\s+clone\b', command):
        found.append('git clone (use a worktree of the existing checkout)')
    for m in re.finditer(r'\bgit\s+(?:-C\s+\S+\s+)?worktree\s+add\s+((?:' + NO_VALUE_FLAGS + r'\s+|-\S+\s+\S+\s+)*)(\S+)', command):
        path = m.group(2)
        name = os.path.basename(os.path.normpath(path))
        if not (path.startswith(WT + '/') and owns(unit, name)):
            found.append(f'worktree {path} is not {WT}/{unit}--<suffix>')
    for m in re.finditer(r'\bgit\s+(?:-C\s+\S+\s+)?worktree\s+(move|remove)\s+(?:(?:--force|-f)\s+)*(\S+)', command):
        name = os.path.basename(os.path.normpath(m.group(2)))
        if not owns(unit, name):
            found.append(f'worktree {m.group(1)} of {m.group(2)}, which is not {unit}\'s checkout')
    verbs = r"(switch|checkout|commit|merge|reset|stash)(?![\w-])"
    for m in [*re.finditer(r'\bcd\s+(' + WT + r'/[^\s/;&|]+)[^;\n]*?\bgit\s+' + verbs, command),
              *re.finditer(r'\bgit\s+-C\s+(' + WT + r'/[^\s/;&|]+)\s+' + verbs, command)]:
        name = os.path.basename(os.path.normpath(m.group(1)))
        if not owns(unit, name) and not name.endswith('-check'):
            found.append(f'git {m.group(2)} in {m.group(1)}, which is not {unit}\'s checkout')
    if re.search(r'\buv\s+(venv|sync)\b|-m\s+venv\b|\bvirtualenv\b', command):
        found.append('creates a virtualenv (use the shared <repo>-check/.venv with PYTHONPATH=src)')
    if any('.artifacts' in t for t in destinations(command)) or re.search(r'--(prefix|target)[= ]\S*\.artifacts', command):
        found.append('writes .artifacts inside a checkout')
    for target in destinations(command):
        if target.startswith('/'):
            found += [v for v in write_violations(unit, target) if '.artifacts' not in v]
    runs = [part for part in re.split(r'&&|\|\||[;|\n]', command) if not re.search(r'(?<!\S)(--help|-h)(?!\S)', part)]  # asking a tool for its help allocates nothing
    if any(ALLOCATES.search(part) for part in runs) and free_gb() < FLOOR_GB:
        found.append(f'allocates disk while {CONFIG.disk} has {free_gb():.1f} GB free (floor {FLOOR_GB} GB)')
    return found


def destinations(command: str) -> list[str]:
    """Paths a shell command writes: every mkdir/touch argument, the last cp/mv/rsync/install argument, tee and > targets."""
    out = [m.group(1) for m in re.finditer(r'>>?\s*([^\s;&|)]+)', command)]
    for part in re.split(r'&&|\|\||[;|\n]', command):
        try:
            words = [w for w in shlex.split(part, posix=True) if not w.startswith('-')]
        except ValueError:
            words = part.split()
        while words and ('=' in words[0] or words[0] in ('sudo', 'cd')):
            words = words[2:] if words[0] == 'cd' else words[1:]
        if not words:
            continue
        verb, args = os.path.basename(words[0]), [w for w in words[1:] if not w.startswith('>')]
        if verb in ('mkdir', 'touch', 'tee'):
            out += args
        elif verb in ('cp', 'mv', 'rsync', 'install', 'ln') and len(args) >= 2:
            out.append(args[-1])
    return out


def write_violations(unit: str, path: str) -> list[str]:
    path = os.path.normpath(path or '')
    if path.startswith(SCRATCH + '/'):
        name = path[len(SCRATCH) + 1:].split('/')[0]
        if not owns(unit, name):
            return [f'writes {path} (only {SCRATCH}/{unit}/ is yours)']
    if '/.artifacts/' in path:
        return [f'writes {path} inside .artifacts']
    return []
