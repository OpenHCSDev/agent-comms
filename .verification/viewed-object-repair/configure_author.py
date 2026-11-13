import json
from pathlib import Path
r=Path.cwd();ops=[]
def p(f,t,a,b):ops.append(dict(operation='patch_target',file_path=str(r/f),target_qualname=t,replacements=[dict(old_source=a,new_source=b)]))
def i(f,s):ops.append(dict(operation='ensure_import',file_path=str(r/f),import_source=s))
def ins(f,t,s):ops.append(dict(operation='insert_before_target',file_path=str(r/f),target_qualname=t,source=s))
f='src/agent_comms/cli_verbs/configure.py'
i(f,'from agent_comms.cli_support.named import NamedCliCommand')
i(f,'from agent_comms.cli_verbs.cli_command import TargetActionCommand')
p(f,'ConfigureCliCommand','NameArgument, NextTurnConfiguration, ConfigurationCommand, declared_name="configure"','NamedCliCommand, NextTurnConfiguration, ConfigurationCommand, TargetActionCommand[ThreadConfiguredResult], declared_name="configure"')
ins(f,'ConfigureCliCommand.apply','''    @classmethod
    def thread_bindings(cls, comms, thread, status, channel, *, catalog):
        return (cls(name=thread.name),)

''')
p(f,'ConfigureCliCommand.apply','def apply(self, ctx: Comms) -> CliResult:','def apply(self, ctx: Comms) -> ThreadConfiguredResult:')
p('src/agent_comms/cli_verbs/cli_commands.py','ThreadConfiguredResult','class ThreadConfiguredResult(CliResult):','class ThreadConfiguredResult(RecordMember, TargetEditResult):')
Path('.verification/viewed-object-repair/configure.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'offer-the-original-thread-configuration-writer','operations':ops}]},indent=2))
