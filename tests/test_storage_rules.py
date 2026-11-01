"""Scratch is configured; checkout membership is read from Git and the filesystem."""

import runpy
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import pytest

from agent_comms.comms import Comms, wire
from agent_comms.errors import RelationViolationError
from agent_comms.nominal.field_codec import FieldCodec, RecordProblems
from agent_comms.repo import Checkout, DetachedStart, Repo, RepositoryReads
from agent_comms.repository_name import RepositoryName
from agent_comms.shell_effects import (
    CreateCheckoutStorageEffect,
    RemoveCheckoutStorageEffect,
    ShellProgram,
    StorageDenied,
    StorageEffect,
    shell_storage_effects,
)
from agent_comms.standing_rules import StorageHomeMissing
from agent_comms.storage_projection import StorageProjection
from agent_comms.storage_rules import (
    ScopedDirectory,
    StorageRule,
    UnitStorage,
)
from agent_comms.thread_storage import (
    StoragePath,
    StorageRoots,
    UnassignedThreadStorage,
)
from agent_comms.threads import Thread
from agent_comms.tool_denial import SelectedToolDenied


def test_isolated_policy_is_durable_and_preserves_existing_private_limits(tmp_path: Path):
    root = tmp_path / "verification"
    configured = StorageRule(floor_gib=0, cleanup_gib=2, memory_gib=1)
    policy = configured.prepare_isolated(root)
    before = StorageRule.path_in(root).read_bytes()
    assert policy == configured.within(root) == StorageRule.load(root)
    policy.require_isolated(root)
    assert StorageRule().prepare_isolated(root) == policy
    assert StorageRule.path_in(root).read_bytes() == before
    reopened = wire(root)
    assert reopened.registry.storage_rule == policy
    assert policy.initial_roots("fork").scratch.is_relative_to(root)
    assert policy.checkout_base.is_relative_to(root)
    assert policy.temp_root.is_relative_to(root)


def test_isolated_preparation_refuses_an_existing_foreign_policy_without_rewriting(tmp_path: Path):
    root = tmp_path / "verification"
    root.mkdir()
    foreign = StorageRule(scratch_base=tmp_path / "not-this-root")
    path = StorageRule.path_in(root)
    path.write_text(FieldCodec.compact(FieldCodec.encode(foreign)))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        StorageRule().prepare_isolated(root)
    assert path.read_bytes() == before
    assert not foreign.scratch_base.exists()


@pytest.mark.parametrize("scoped_directory", [
    pytest.param(directory, id=directory.value.as_posix())
    for plan in FieldCodec.field_plans(StorageRule)
    for directory in ScopedDirectory.single(plan.annotation)
    if directory.value.parts
])
class TestIsolatedDirectories:
    def test_projection_refuses_a_canonical_escape_before_writing(
        self, tmp_path: Path, scoped_directory: ScopedDirectory,
    ):
        root = tmp_path / "verification"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / scoped_directory.value).symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="escapes"):
            StorageRule().within(root)
        with pytest.raises(ValueError, match="escapes"):
            StorageRule().prepare_isolated(root)
        assert not StorageRule.path_in(root).exists()
        assert tuple(outside.iterdir()) == ()

    def test_existing_policy_rejects_a_new_escape_without_rewriting(
        self, tmp_path: Path, scoped_directory: ScopedDirectory,
    ):
        root = tmp_path / "verification"
        policy = StorageRule(floor_gib=0).prepare_isolated(root)
        policy_path = StorageRule.path_in(root)
        before = policy_path.read_bytes()
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / scoped_directory.value).symlink_to(outside, target_is_directory=True)
        loaded = StorageRule.load(root)
        for current in (policy, loaded):
            with pytest.raises(ValueError, match="escapes"):
                current.require_isolated(root)
        with pytest.raises(ValueError, match="escapes"):
            StorageRule().prepare_isolated(root)
        assert policy_path.read_bytes() == before
        assert tuple(outside.iterdir()) == ()

    def test_inward_links_preserve_private_scope(
        self, tmp_path: Path, scoped_directory: ScopedDirectory,
    ):
        root = tmp_path / "verification"
        root.mkdir()
        destination = root / "inward"
        destination.mkdir()
        (root / scoped_directory.value).symlink_to(destination, target_is_directory=True)
        policy = StorageRule(floor_gib=0).prepare_isolated(root)
        policy.require_isolated(root)
        assert StorageRule.load(root) == policy
        assert StorageRule().prepare_isolated(root) == policy


def test_future_child_cannot_follow_an_escaping_scratch_link(tmp_path: Path):
    policy = StorageRule(floor_gib=0).prepare_isolated(tmp_path / "verification")
    policy.scratch_base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (policy.scratch_base / "future-child").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="escapes"):
        policy.initial_roots("future-child")
    assert tuple(outside.iterdir()) == ()


def test_private_projection_uses_new_field_declarations_without_another_roster(tmp_path: Path):
    @dataclass(frozen=True)
    class ExtendedStorageRule(StorageRule):
        output: Annotated[StoragePath, ScopedDirectory(Path("output"))] = Path("/external")

    scoped = ExtendedStorageRule(floor_gib=0).within(tmp_path)
    assert scoped.output == tmp_path / "output"
    scoped.require_isolated(tmp_path)
    assert FieldCodec.decode(ExtendedStorageRule, FieldCodec.encode(scoped)) == scoped


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


@pytest.mark.parametrize("separator", ("--", "__"))
def test_allocation_names_invert_every_declared_checkout_name(tmp_path, monkeypatch, separator):
    """The checkout name format has one declaration: renaming its separator renames every
    checkout and its inverse together."""
    monkeypatch.setattr(StorageRule, "checkout_separator", separator)
    rule = StorageRule(checkout_base=tmp_path / "wt")
    for allocation in ("pi-unit", f"pi{separator}unit"):
        roots = StorageRoots(tmp_path / "scratch" / allocation)
        checkout = rule.order_checkout(roots, "P7")
        companion = rule.repository_checkout(roots, "P7", RepositoryName("openhcsdev", "toad"))
        assert separator in checkout.name
        for named in (checkout, companion):
            assert allocation in tuple(rule.allocation_names(named))
    assert tuple(rule.allocation_names(tmp_path / f"pi-unit{separator}P7")) == ()


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
    thread = Thread('worker', frozenset(), Checkout(tmp_path / 'home'))
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
    comms.registry.declare(Thread('worker', frozenset(), Checkout(home)))
    return comms


def test_removed_worktree_updates_membership_and_another_home_cannot_claim_its_parent(
        checkout_comms: Comms, tmp_path: Path):
    comms = checkout_comms
    rule = comms.registry.storage_rule
    home, old, new = (rule.checkout_base / name for name in ('worker', 'worker--P1', 'worker--P2'))
    Checkout.add(home, old, DetachedStart('HEAD'))
    thread = comms.registry.require('worker')
    other = comms.registry.declare(Thread('other', frozenset(), Checkout(tmp_path)))
    assert rule.checkouts_of(thread, RepositoryReads()) == (home, old)
    storage = UnitStorage.read(rule, thread, RepositoryReads())
    scratch = rule.roots_of(thread).scratch
    scratch.mkdir(parents=True)
    (scratch / 'payload').write_bytes(b's' * 128 * 2**10)
    (old / 'payload').write_bytes(b'c' * 128 * 2**10)
    storage = UnitStorage.read(rule, thread, RepositoryReads())
    assert storage.home == Checkout(home)
    assert storage.extra_checkouts == (Checkout(old),)
    assert storage.adding(Checkout(home)) == storage
    assert rule.owns_present_checkout(thread, home, RepositoryReads())
    assert not rule.owns_present_checkout(other, old, RepositoryReads())
    with pytest.raises(StorageDenied):
        StorageProjection(rule, other, comms).check((
            CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    StorageProjection(rule, thread, comms).check((
        CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    StorageProjection(rule, thread, comms).check((RemoveCheckoutStorageEffect(old),))
    shutil.rmtree(old)
    assert rule.checkouts_of(thread, RepositoryReads()) == (home,)
    StorageProjection(rule, thread, comms).check((
        CreateCheckoutStorageEffect(new, Repo.of(home), DetachedStart('HEAD')),))
    assert comms.registry.require('worker').storage_roots == thread.storage_roots


def test_equal_repository_spellings_allocate_one_owned_checkout(checkout_comms: Comms):
    """RepositoryName equality is casefolded, so the CLI's spelling and the registered one name
    the same allocation, and an existing lowercase checkout stays where it is."""
    comms = checkout_comms
    rule = comms.registry.storage_rule
    thread = comms.registry.require('worker')
    home = rule.checkout_base / 'worker'
    typed = RepositoryName('OpenHCSDev', 'Agent-Comms')
    registered = RepositoryName.from_wire('openhcsdev/agent-comms')
    assert typed == registered
    path = rule.repository_checkout(rule.roots_of(thread), 'P1', typed)
    assert path == rule.checkout_base / 'worker--P1--agent-comms'
    assert path == rule.repository_checkout(rule.roots_of(thread), 'P1', registered)
    start = DetachedStart('HEAD')
    StorageProjection(rule, thread, comms).check((
        CreateCheckoutStorageEffect(path, Repo.of(home), start),))
    Checkout.add(home, path, start)
    assert rule.owns_present_checkout(thread, path, RepositoryReads())
    assert path in rule.checkouts_of(thread, RepositoryReads())


@pytest.mark.parametrize('enclosing_source', (False, True))
def test_a_thread_registered_above_its_scratch_may_write_its_scratch_and_only_that(
        checkout_comms: Comms, enclosing_source: bool):
    """An enclosing project or Git tree cannot take the registry's scratch allocation."""
    comms = checkout_comms
    rule = comms.registry.storage_rule
    above = rule.scratch_base.parent
    if enclosing_source:
        Repo.git(above, 'init', '-q')
    thread = comms.registry.declare(Thread('homed', frozenset(), Checkout(above)))
    projection = StorageProjection(rule, thread, comms)
    scratch = rule.roots_of(thread).scratch
    projection.require_write(scratch / 'compaction-deal.audit.json')
    with pytest.raises(StorageDenied, match='not allocated editable work'):
        projection.require_write(above / 'notes.txt')
    with pytest.raises(StorageDenied, match='foreign scratch'):
        projection.require_write(rule.scratch_base / 'worker' / 'notes.txt')
    with pytest.raises(StorageDenied, match='artifacts'):
        projection.require_write(scratch / '.artifacts' / 'output')
    scratch.mkdir(parents=True)
    (scratch / 'project-link').symlink_to(above, target_is_directory=True)
    with pytest.raises(StorageDenied, match='not allocated editable work'):
        projection.require_write(scratch / 'project-link' / 'notes.txt')


@pytest.mark.parametrize('source_path', (Path(), Path('nested-source')))
def test_git_source_inside_scratch_still_requires_an_edit_allocation(
        checkout_comms: Comms, source_path: Path):
    comms = checkout_comms
    rule = comms.registry.storage_rule
    above = rule.scratch_base.parent
    thread = comms.registry.declare(Thread('homed', frozenset(), Checkout(above)))
    scratch = rule.roots_of(thread).scratch
    source = scratch / source_path
    source.mkdir(parents=True)
    Repo.git(source, 'init', '-q')
    with pytest.raises(StorageDenied, match='not allocated editable work'):
        StorageProjection(rule, thread, comms).require_write(source / 'implementation.py')


def test_three_small_checkouts_are_admitted(checkout_comms: Comms):
    comms = checkout_comms
    rule = comms.registry.storage_rule
    thread = checkout_comms.registry.require('worker')
    projection = StorageProjection(rule, thread, comms)
    for problem in ('P1', 'P2', 'P3'):
        path = rule.order_checkout(rule.roots_of(thread), problem)
        start = DetachedStart('HEAD')
        projection.check((CreateCheckoutStorageEffect(path, Repo.of(thread.project_path), start),))
        Checkout.add(thread.project_path, path, start)
    storage = UnitStorage.read(rule, thread, RepositoryReads())
    assert len(storage.extra_checkouts) == 3


def test_projected_checkout_membership_follows_add_and_remove(checkout_comms: Comms):
    rule = checkout_comms.registry.storage_rule
    thread = checkout_comms.registry.require('worker')
    storage = UnitStorage.read(rule, thread, RepositoryReads())
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
    thread = comms.registry.declare(Thread('worker', frozenset(), Checkout(project_link)))
    assert thread.project_path == current
    assert UnitStorage.read(comms.registry.storage_rule, thread, RepositoryReads()).home == Checkout(current)
    with pytest.raises(StorageDenied):
        StorageProjection(comms.registry.storage_rule, comms.registry.require('worker'), comms).check((
            RemoveCheckoutStorageEffect(project_link.resolve()),))
    assert current.is_dir()
    StorageProjection(comms.registry.storage_rule, comms.registry.require('worker'), comms).check((
        RemoveCheckoutStorageEffect(other),))
    subprocess.run(('git', '-C', str(repository), 'worktree', 'remove', str(other)), check=True)
    assert not other.exists()
    assert current.is_dir()


@pytest.mark.parametrize('command', ['git clone remote target',
                                     'git worktree add target origin/main',
                                     'git worktree remove --force target',
                                     'python -m venv env', 'uv sync',
                                     'git -C ~/wt/other--P3 reset --hard', 'touch ~/wt/x-check/file'])
def test_forbidden_shell_operations_use_declared_rule(tmp_path, command, comms):
    rule = StorageRule()
    thread = Thread('worker', frozenset(), Checkout(tmp_path), storage_roots=rule.initial_roots('worker'))
    with pytest.raises(SelectedToolDenied):
        StorageProjection(rule, thread, comms).check(shell_storage_effects(command, tmp_path))


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
    storage = UnitStorage.read(comms.registry.storage_rule, thread, RepositoryReads())
    assert storage.unit == thread.incarnation
    assert StorageHomeMissing(storage).observation_key() == StorageHomeMissing(
        UnitStorage.read(comms.registry.storage_rule, thread, RepositoryReads())).observation_key()
