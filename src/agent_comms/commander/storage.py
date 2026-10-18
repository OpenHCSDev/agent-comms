"""Storage rules for fleet units, one owner for every check: units never clean up after themselves.

Each unit owns at most MAX_WORKTREES checkouts named <worktrees>/<unit> or <worktrees>/<unit>--<suffix>, and one
scratch directory <scratch>/<unit>/. Tests run on the shared virtualenv of the <repo>-check checkout with
PYTHONPATH=src; no per-worktree venv, no .artifacts, no clone. Below FLOOR_GB free on the configured disk nothing
that allocates disk may run. order (send/done/overdue) enforces through this module; tool_watch through
Core's StorageRule.
"""
import os, re, shutil, subprocess

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
