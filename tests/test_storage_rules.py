"""Scratch is configured; checkout membership is read from Git and the filesystem."""

import runpy
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.field_codec import FieldCodec, RecordProblems
from agent_comms.repo import Checkout, DetachedStart, Repo
from agent_comms.shell_effects import (
    CreateCheckoutStorageEffect,
    RemoveCheckoutStorageEffect,
    ShellProgram,
    StorageDenied,
    StorageEffect,
    shell_storage_effects,
)
from agent_comms.standing_rules import StorageHomeMissing
from agent_comms.storage_rules import (
    StorageProjection,
    StorageRule,
    UnitStorage,
)
from agent_comms.thread_storage import (
    StorageRoots,
    UnassignedThreadStorage,
)
from agent_comms.threads import Thread
from agent_comms.tool_denial import SelectedToolDenied


def test_resource_checker_uses_storage_allocation_floor(tmp_path):
    checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools' / 'agent_resource_check.py'))
    floor = StorageRule.load(tmp_path).floor_gib
    limits = checker['limits_for'](tmp_path)
    assert limits['/home'][1] == floor
    free = {path: 100 * checker['GIB'] for path in limits}
    memory = {'MemAvailable': 100 * checker['GIB'], 'SwapTotal': 0, 'SwapFree': 0, 'full_avg60': 0}
    free['/home'] = (floor - 0.1) * checker['GIB']
    assert checker['severity'](free, memory, limits)[0] == 'critical'
    free['/home'] = (floor + 0.1) * checker['GIB']
    assert checker['severity'](free, memory, limits)[0] == 'warning'


def test_resource_checker_reads_root_override(tmp_path):
    import json
    checker = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'tools' / 'agent_resource_check.py'))
    floor = StorageRule.load(tmp_path).floor_gib
    (tmp_path / 'storage.json').write_text(json.dumps({'floor_gib': floor + 1}))
    assert checker['limits_for'](tmp_path)['/home'][1] == StorageRule.load(tmp_path).floor_gib


def test_storage_paths_round_trip_inside_collections(tmp_path):
    roots = StorageRoots(tmp_path / 'scratch')
    encoded = FieldCodec.encode(roots)
    assert encoded == {'scratch': str(roots.scratch)}
    assert FieldCodec.decode(StorageRoots, encoded) == roots
    with pytest.raises(ValueError):
        FieldCodec.decode(StorageRoots, {**encoded, 'checkouts': []})


def test_registry_assigns_a_new_thread_its_roots_at_declaration(tmp_path):
    import json
    (tmp_path / 'storage.json').write_text(json.dumps({
        'scratch_base': str(tmp_path / 'scratch'), 'floor_gib': 47,
    }))
    comms = wire(tmp_path)
    rule = comms.registry.storage_rule
    thread = Thread('worker', frozenset(), str(tmp_path / 'home'))
    assert thread.storage_roots == UnassignedThreadStorage()
    assert rule.floor_gib == 47
    declared = comms.registry.declare(thread)
    assert declared.storage_roots == StorageRoots(tmp_path / 'scratch' / 'worker')
    assert comms.registry.require('worker').storage_roots == declared.storage_roots
    stored = declared.to_wire()
    assert FieldCodec.decode(Thread, stored) == declared
    with pytest.raises(RecordProblems):
        FieldCodec.decode(Thread, {**stored, 'storage_roots': None})
    with pytest.raises(RelationViolationError):
        thread.to_wire()


@pytest.fixture
def checkout_comms(tmp_path: Path) -> Comms:
    base = tmp_path / 'checkouts'
    home = base / 'worker'
    home.mkdir(parents=True)
    Repo.git(home, 'init', '-q')
    Repo.git(home, 'config', 'user.name', 'storage-proof')
    Repo.git(home, 'config', 'user.email', 'storage@example.invalid')
    Repo.git(home, 'remote', 'add', 'origin', 'https://github.com/OpenHCSDev/agent-comms.git')
    Repo.git(home, 'commit', '-q', '--allow-empty', '-m', 'seed')
    comms = wire(tmp_path / 'wire')
    comms.registry.storage_rule = StorageRule(
        checkout_base=base, scratch_base=tmp_path / 'scratch', floor_gib=0)
    comms.registry.declare(Thread('worker', frozenset(), str(home)))
    return comms


def test_removed_worktree_updates_membership_and_another_home_cannot_claim_its_parent(
        checkout_comms: Comms, tmp_path: Path):
    comms = checkout_comms
    rule = comms.registry.storage_rule
    home, old, new = (rule.checkout_base / name for name in ('worker', 'worker--P1', 'worker--P2'))
    Checkout.add(home, old, DetachedStart('HEAD'))
    thread = comms.registry.require('worker')
    other = comms.registry.declare(Thread('other', frozenset(), str(tmp_path)))
    assert rule.checkouts_of(thread) == (home, old)
    storage = UnitStorage.read(rule, thread)
    scratch = rule.roots_of(thread).scratch
    scratch.mkdir(parents=True)
    (scratch / 'payload').write_bytes(b's' * 128 * 2**10)
    (old / 'payload').write_bytes(b'c' * 128 * 2**10)
    storage = UnitStorage.read(rule, thread)
    assert storage.home == Checkout(home)
    assert storage.extra_checkouts == (Checkout(old),)
    assert storage.adding(Checkout(home)) == storage
    assert rule.owns_present_checkout(thread, home)
    assert not rule.owns_present_checkout(other, old)
    with pytest.raises(StorageDenied):
        StorageProjection(rule, other).check((
            CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    StorageProjection(rule, thread).check((
        CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    StorageProjection(rule, thread).check((RemoveCheckoutStorageEffect(old),))
    shutil.rmtree(old)
    assert rule.checkouts_of(thread) == (home,)
    StorageProjection(rule, thread).check((
        CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    assert comms.registry.require('worker').storage_roots == thread.storage_roots


def test_three_small_checkouts_are_admitted(checkout_comms: Comms):
    rule = checkout_comms.registry.storage_rule
    thread = checkout_comms.registry.require('worker')
    projection = StorageProjection(rule, thread)
    for problem in ('P1', 'P2', 'P3'):
        path = rule.order_checkout(rule.roots_of(thread), problem)
        start = DetachedStart('HEAD')
        projection.check((CreateCheckoutStorageEffect(path, Repo.of(thread.project_path), start),))
        Checkout.add(thread.project_path, path, start)
    storage = UnitStorage.read(rule, thread)
    assert len(storage.extra_checkouts) == 3


def test_projected_checkout_membership_follows_add_and_remove(checkout_comms: Comms):
    rule = checkout_comms.registry.storage_rule
    thread = checkout_comms.registry.require('worker')
    storage = UnitStorage.read(rule, thread)
    first = Checkout(rule.order_checkout(rule.roots_of(thread), 'P1'))
    second = Checkout(rule.order_checkout(rule.roots_of(thread), 'P2'))
    single = storage.adding(first)
    assert single.removing(first) == storage
    assert single.adding(first) == single
    both = single.adding(second)
    assert both.removing(first).extra_checkouts == (second,)
    assert both.adding(first) == both


def test_cannot_remove_current_project_but_can_remove_another_checkout(tmp_path):
    base = tmp_path / 'checkouts'
    repository, current, other = base / 'repository', base / 'worker--P1', base / 'worker--P2'
    repository.mkdir(parents=True)
    subprocess.run(('git', 'init', '-q', str(repository)), check=True)
    subprocess.run(('git', '-C', str(repository), '-c', 'user.name=P356',
                    '-c', 'user.email=p356@example.invalid', 'commit', '-q',
                    '--allow-empty', '-m', 'seed'), check=True)
    for checkout in (current, other):
        subprocess.run(('git', '-C', str(repository), 'worktree', 'add', '-q', str(checkout)), check=True)
    project_link = tmp_path / 'current-project'
    project_link.symlink_to(current, target_is_directory=True)
    comms = wire(tmp_path / 'wire')
    comms.registry.storage_rule = StorageRule(checkout_base=base, scratch_base=tmp_path / 'scratch')
    thread = comms.registry.declare(Thread('worker', frozenset(), str(project_link)))
    assert thread.project_path == current
    assert UnitStorage.read(comms.registry.storage_rule, thread).home == Checkout(current)
    with pytest.raises(StorageDenied):
        StorageProjection(comms.registry.storage_rule, comms.registry.require('worker')).check((
            RemoveCheckoutStorageEffect(project_link.resolve()),))
    assert current.is_dir()
    StorageProjection(comms.registry.storage_rule, comms.registry.require('worker')).check((
        RemoveCheckoutStorageEffect(other),))
    subprocess.run(('git', '-C', str(repository), 'worktree', 'remove', str(other)), check=True)
    assert not other.exists()
    assert current.is_dir()


@pytest.mark.parametrize('command', ['git clone remote target',
                                     'git worktree add target origin/main',
                                     'git worktree remove --force target',
                                     'python -m venv env', 'uv sync',
                                     'git -C ~/wt/other--P3 reset --hard', 'touch ~/wt/x-check/file'])
def test_forbidden_shell_operations_use_declared_rule(tmp_path, command):
    rule = StorageRule()
    thread = Thread('worker', frozenset(), str(tmp_path), storage_roots=rule.initial_roots('worker'))
    with pytest.raises(SelectedToolDenied):
        StorageProjection(rule, thread).check(shell_storage_effects(command, tmp_path))


def test_programs_do_not_claim_to_be_effect_values():
    assert all(not issubclass(program, StorageEffect)
               for program in set(ShellProgram.programs.values()))


@pytest.mark.parametrize('command,kind', [
    ('python3.14.7 -m venv env', 'ForbiddenStorageEffect'),
    ('python /scripts/build_runtime.py', 'BuildStorageEffect'),
    ('python -m pip install package', 'AllocateStorageEffect'),
    ('uv pip install package', 'AllocateStorageEffect'),
    ('npm ci', 'AllocateStorageEffect'),
    ('ffmpeg -i input output', 'AllocateStorageEffect'),
    ('git -C repo worktree add --detach extra origin/main', 'ForbiddenStorageEffect'),
    ('git worktree remove --force extra', 'ForbiddenStorageEffect'),
    ('git worktree move extra elsewhere', 'ForbiddenStorageEffect'),
    ('git -C ../other--P3 reset --hard', 'WriteStorageEffect'),
    ('pip install --help\nmkdir target', 'WriteStorageEffect'),
    ('mkdir -p target', 'WriteStorageEffect'),
    ('cp source target', 'WriteStorageEffect'),
    ("cat <<'PY'\ngit clone remote target\nPY\nmkdir target", 'WriteStorageEffect'),
    ("echo '<<' 'EOF'\npython -m venv env", 'ForbiddenStorageEffect'),
    ("cat <<'A' <<'B'\ngit clone remote target\nA\nuv sync\nB\nmkdir target", 'WriteStorageEffect'),
    ("echo 'git clone remote target'\nmkdir target", 'WriteStorageEffect'),
    ("python -c 'import sys\nprint(\"<<\")'\nmkdir target", 'WriteStorageEffect'),
    ('echo "first\nsecond" > target', 'WriteStorageEffect'),
])
def test_program_decoders_preserve_command_shapes(tmp_path, command, kind):
    effects = shell_storage_effects(command, tmp_path)
    assert len(effects) == 1
    assert type(effects[0]).__name__ == kind


def test_an_unterminated_quote_is_a_declared_refusal(tmp_path):
    with pytest.raises(StorageDenied):
        shell_storage_effects("echo 'never closed\nmkdir target", tmp_path)


def test_storage_violation_observation_key_resolves_every_compared_field(checkout_comms: Comms):
    """The heartbeat digests a storage rule through each field's declared type."""
    comms = checkout_comms
    thread = comms.registry.require('worker')
    storage = UnitStorage.read(comms.registry.storage_rule, thread)
    assert storage.unit == thread.incarnation
    assert StorageHomeMissing(storage).observation_key() == StorageHomeMissing(
        UnitStorage.read(comms.registry.storage_rule, thread)).observation_key()
