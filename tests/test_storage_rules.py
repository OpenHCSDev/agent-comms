"""Scratch is configured; checkout membership is read from Git and the filesystem."""
import shutil
import subprocess
from pathlib import Path
import runpy

import pytest

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.native_tool_call import SelectedToolDenied
from agent_comms.storage_rules import (
    AllocateStorageEffect, CheckoutStorageEffect, RemoveCheckoutStorageEffect, StorageRoots, StorageRule,
    StorageProjection, WriteStorageEffect, shell_storage_effects,
)
from agent_comms.threads import Thread


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


def test_registry_rule_supplies_new_thread_roots(tmp_path):
    import json
    (tmp_path / 'storage.json').write_text(json.dumps({
        'scratch_base': str(tmp_path / 'scratch'), 'floor_gib': 47,
    }))
    comms = Comms(tmp_path)
    rule = comms.registry.storage_rule
    thread = comms.registry.new_thread('worker', frozenset(), str(tmp_path / 'home'))
    assert thread.storage_roots.scratch == tmp_path / 'scratch' / 'worker'
    assert rule.floor_gib == 47
    assert comms.registry.declare(thread).storage_roots == thread.storage_roots


def test_removed_worktree_frees_quota_and_another_home_cannot_claim_its_parent(tmp_path, monkeypatch):
    base = tmp_path / 'checkouts'
    home, old, new = base / 'worker', base / 'worker--P1', base / 'worker--P2'
    home.mkdir(parents=True)
    subprocess.run(('git', 'init', '-q', str(home)), check=True)
    subprocess.run(('git', '-C', str(home), '-c', 'user.name=P430', '-c', 'user.email=p430@example.invalid',
                    'commit', '-q', '--allow-empty', '-m', 'seed'), check=True)
    subprocess.run(('git', '-C', str(home), 'worktree', 'add', '-q', str(old)), check=True)
    rule = StorageRule(checkout_base=base, scratch_base=tmp_path / 'scratch', floor_gib=0, max_checkouts=1)
    comms = Comms(tmp_path / 'wire')
    comms.registry.storage_rule = rule
    thread = comms.registry.declare(comms.registry.new_thread('worker', frozenset(), str(home)))
    comms.registry.declare(comms.registry.new_thread('opencode', frozenset(), '/home/ts'))
    assert rule.checkouts_of(thread) == (home, old)
    with pytest.raises(SelectedToolDenied, match='at most 1'):
        comms.registry.decide_storage('worker', (CheckoutStorageEffect(new),))
    comms.registry.decide_storage('worker', (RemoveCheckoutStorageEffect(old),))
    shutil.rmtree(old)
    assert rule.checkouts_of(thread) == (home,)
    comms.registry.decide_storage('worker', (CheckoutStorageEffect(new),))
    assert comms.registry.require('worker').storage_roots == thread.storage_roots


@pytest.mark.parametrize('command', ['git clone remote target', 'python -m venv env', 'uv sync',
                                     'git -C ~/wt/other--P3 reset --hard', 'touch ~/wt/x-check/file'])
def test_forbidden_shell_operations_use_declared_rule(tmp_path, command):
    rule = StorageRule()
    thread = Thread('worker', frozenset(), str(tmp_path), storage_roots=rule.initial_roots('worker'))
    with pytest.raises(SelectedToolDenied):
        projection = StorageProjection(rule, thread)
        for effect in shell_storage_effects(command, tmp_path):
            effect.check(projection)


def test_programs_do_not_claim_to_be_effect_values():
    from agent_comms.storage_rules import ShellProgram, StorageEffect
    assert all(not issubclass(program, StorageEffect)
               for program in set(ShellProgram.programs.values()))


@pytest.mark.parametrize('command,kind', [
    ('python3.14.7 -m venv env', 'ForbiddenStorageEffect'),
    ('python /scripts/build_runtime.py', 'BuildStorageEffect'),
    ('python -m pip install package', 'AllocateStorageEffect'),
    ('uv pip install package', 'AllocateStorageEffect'),
    ('npm ci', 'AllocateStorageEffect'),
    ('ffmpeg -i input output', 'AllocateStorageEffect'),
    ('git -C repo worktree add --detach extra origin/main', 'CheckoutStorageEffect'),
    ('git worktree remove --force extra', 'RemoveCheckoutStorageEffect'),
    ('git worktree move extra elsewhere', 'ForbiddenStorageEffect'),
    ('git -C ../other--P3 reset --hard', 'WriteStorageEffect'),
    ('pip install --help\nmkdir target', 'WriteStorageEffect'),
    ('mkdir -p target', 'WriteStorageEffect'),
    ('cp source target', 'WriteStorageEffect'),
    ("cat <<'PY'\ngit clone remote target\nPY\nmkdir target", 'WriteStorageEffect'),
    ("echo '<<' 'EOF'\npython -m venv env", 'ForbiddenStorageEffect'),
    ("cat <<'A' <<'B'\ngit clone remote target\nA\nuv sync\nB\nmkdir target", 'WriteStorageEffect'),
    ("echo 'git clone remote target'\nmkdir target", 'WriteStorageEffect'),
])
def test_program_decoders_preserve_command_shapes(tmp_path, command, kind):
    effects = shell_storage_effects(command, tmp_path)
    assert len(effects) == 1
    assert type(effects[0]).__name__ == kind
