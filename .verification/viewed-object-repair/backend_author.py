import json
from pathlib import Path
root=Path.cwd();ops=[]
def patch(f,t,o,n):ops.append(dict(operation='patch_target',file_path=str(root/f),target_qualname=t,replacements=[dict(old_source=o,new_source=n)]))
def insert(f,t,s):ops.append(dict(operation='insert_before_target',file_path=str(root/f),target_qualname=t,source=s))
def imp(f,s):ops.append(dict(operation='ensure_import',file_path=str(root/f),import_source=s))
f='src/agent_comms/cli_verbs/cli_command.py'
insert(f,'CliCommand.catalog_declaration','''    @classmethod
    def configuration_actions(cls, action: TargetAction) -> tuple[TargetAction, ...]:
        """Ordinary operations do not edit the selected object's configuration."""
        return ()

''')
insert(f,'TargetAction','''class ConfigurationCommand(CliCommand, declared_name=None):
    """A command whose original parameters are the selected object's settings."""

    @classmethod
    def configuration_actions(cls, action: TargetAction) -> tuple[TargetAction, ...]:
        return (action,)


''')
for f,target,old,new in (
 ('src/agent_comms/cli_verbs/configure.py','ConfigureCliCommand','NameArgument, NextTurnConfiguration, CliCommand,','NameArgument, NextTurnConfiguration, ConfigurationCommand,'),
 ('src/agent_comms/cli_verbs/rule_gate.py','RuleGateCommand','RuleGateSettings, CliCommand,','RuleGateSettings, ConfigurationCommand,')):
 imp(f,'from agent_comms.cli_verbs.cli_command import ConfigurationCommand')
 patch(f,target,old,new)
Path('.verification/viewed-object-repair/backend.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'configuration-membership-on-the-original-command','operations':ops}]},indent=2))
