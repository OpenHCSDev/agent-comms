"""The storage rule as the commander states it to fleet units, and the disk's free space.

Core's StorageRule owns every check: which checkouts a unit owns, its quota and the allocation floor.
"""
import shutil

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


def free_gb() -> float:
    return shutil.disk_usage(CONFIG.disk).free / 2**30
