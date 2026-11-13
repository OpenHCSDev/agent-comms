import json
from pathlib import Path
root=Path.cwd(); target=root/'tests/test_checkout_submodules.py'
src='''def test_adoption_keeps_retired_tree_and_live_recipient_in_place(checkout_command_bus, repository):
    comms = checkout_command_bus
    rule = comms.registry.storage_rule
    recipient = comms.registry.require('worker')
    path = rule.order_checkout(rule.initial_roots('departed'), 'P4')
    path.parent.mkdir(parents=True, exist_ok=True)
    source = Checkout.add(repository, path, NewBranchStart('retired-adoption', 'HEAD'))
    departed = comms.registry.declare(ExternalThread('departed', frozenset(), source))
    (path / 'owner.py').write_text('staged bytes\\n')
    git(path, 'add', 'owner.py')
    (path / 'owner.py').write_text('unstaged bytes\\n')
    (path / 'untracked.bin').write_bytes(b'\\x00retained\\xff')
    before = git(path, 'status', '--porcelain'), git(path, 'rev-parse', 'HEAD')
    arguments = ('--root', str(comms.root), 'checkout', '--from', 'worker',
                 'adopt', 'P1', '--source', str(path))
    refused = core_process(*arguments)
    assert refused.returncode != 0
    comms.registry.register(departed, ArchivedThreadStatus())
    adopted = core_process(*arguments)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    current = comms.registry.require('worker')
    previous = comms.registry.require('departed')
    assert current.worktree == recipient.worktree and current.goal == recipient.goal
    assert previous.worktree == departed.worktree
    assert rule.owns_editable_checkout(current, path, comms.goals.todos.get('P1').repo, RepositoryReads())
    assert not rule.owns_present_checkout(previous, path, RepositoryReads())
    assert path in rule.checkout_paths(current, RepositoryReads())
    assert rule.reused_checkout(current, comms.goals.todos.get('P1').repo,
                                rule.order_checkout(rule.roots_of(current), 'P1'), RepositoryReads()) == path
    assert (git(path, 'status', '--porcelain'), git(path, 'rev-parse', 'HEAD')) == before
    assert (path / 'untracked.bin').read_bytes() == b'\\x00retained\\xff'


'''
ops=[dict(operation='insert_before_target',file_path=str(target),target_qualname='test_public_transfer_keeps_dirty_index_branch_modules_and_current_thread',source=src),dict(operation='ensure_import',file_path=str(target),import_source='from agent_comms.registry.thread_status import ArchivedThreadStatus')]
Path('.verification/retired-checkout-adoption/control.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'real-retired-checkout-control','operations':ops}]},indent=2))
