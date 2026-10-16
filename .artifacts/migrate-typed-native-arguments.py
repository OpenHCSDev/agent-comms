"""Revision-checked exact migration after the NRA owner/consumer inventory.

This applies admitted typed-argument moves, not a behavioral-equivalence proof.
The CLI main parser is the sole NativePi parse boundary; launch consumers take
NativeArguments. Fixtures/embedded source are a separate reported phase.
"""
from pathlib import Path
from nominal_refactor_advisor.ast_tools import parse_python_module_roots, python_module_path_identities_for_roots
root = Path('/home/ts/wt/core-native-runtime')
modules = parse_python_module_roots((root/'src',), use_parse_cache=False, parse_workers=1)
parsed = {module.path.resolve() for module in modules}
missing = [str(row.path) for row in python_module_path_identities_for_roots((root/'src',))
           if row.path.resolve() not in parsed]
assert not missing, missing
print('production modules', len(modules), 'omissions', missing)
moves = {
 'native_pi.py': [('arguments: tuple[str, ...]', 'arguments: NativeArguments'),
                  ('NativeArguments.parse(arguments).rpc()', 'arguments.rpc()')],
 'tracked_turn.py': [('arguments: tuple[str, ...]', 'arguments: NativeArguments')],
 'backend.py': [('agent_args: Sequence[str]', 'agent_args: NativeArguments'), ('tuple(agent_args)', 'agent_args')],
 'native_session_prepare.py': [('agent_args: Sequence[str]', 'agent_args: NativeArguments'), ('tuple(agent_args)', 'agent_args')],
 'pi_commands.py': [('arguments: Sequence[str]', 'arguments: NativeArguments'), ('tuple(arguments)', 'arguments')],
 'config_options.py': [('self.comms.settings.parsed_arguments', 'self.comms.settings.arguments'),
                       ('.with_model(thread.model).argv', '.with_model(thread.model)'),
                       ('self.comms, arguments.argv, worktree=', 'self.comms, arguments, worktree=')],
 'pi_native_backend.py': [('self.comms.settings.parsed_arguments', 'self.comms.settings.arguments'),
                        ('def arguments(self, thread: Thread) -> tuple[str, ...]:',
                         'def arguments(self, thread: Thread) -> NativeArguments:'),
                        ('        ).argv', '        )'), ('            ).argv', '            )')],
}
for module in modules:
    if module.path.name not in moves or module.path.parent.name != 'agent_comms':
        continue
    original = module.path.read_text()
    changed = original
    for old, new in moves[module.path.name]:
        print(module.path.name, old, changed.count(old))
        changed = changed.replace(old,new)
    if module.path.name in {'tracked_turn.py', 'backend.py', 'native_session_prepare.py', 'pi_commands.py'}:
        if 'from .native_arguments import NativeArguments' not in changed:
            at = changed.index('\nfrom .', changed.index('from __future__'))
            changed = changed[:at] + '\nfrom .native_arguments import NativeArguments\n' + changed[at:]
    assert module.path.read_text() == original
    module.path.write_text(changed)
print('LIMITS: fixtures and embedded Python require migration; dynamic binding is not inferred.')
