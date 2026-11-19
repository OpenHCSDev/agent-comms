import json
from pathlib import Path
root=Path.cwd()
ops=[]
def patch(file,target,old,new):
    ops.append(dict(operation='patch_target',file_path=str(root/file),target_qualname=target,replacements=[dict(old_source=old,new_source=new)]))
def imp(file,source):
    ops.append(dict(operation='ensure_import',file_path=str(root/file),import_source=source))
f='tests/test_json_flag_record.py'
imp(f,'from agent_comms.registry.thread_settings import ThreadSettings')
imp(f,'from agent_comms.execution.pi_vocabulary import ServiceTier')
# The original fixture exercises explicit nondefault configuration through both transports.
ops.append(dict(operation='patch_target',file_path=str(root/f),target_qualname='selected_record',decorators='include',replacements=[dict(old_source="NativeThreadDefaults(model=DeclaredModel('selected/model'))",new_source="NativeThreadDefaults(model=DeclaredModel('selected/model'), service_tier=ServiceTier.PRIORITY)")]))
patch(f,'test_settings_schema_still_comes_only_from_its_declared_data_fields','''    assert set(FieldCodec.record_schema(NativeThreadDefaults)["properties"]) == {
        "model", "thinking_level",
    }''','''    assert FieldCodec.record_schema(NativeThreadDefaults)["properties"].keys() == (
        FieldCodec.record_schema(ThreadSettings)["properties"].keys())''')
f='tests/test_command_families.py'
patch(f,'test_cli_decodes_tag_sets_enums_json_and_shell_words_once','''        assert configured.service_tier.tier is tier''','''        assert configured.service_tier.tier is tier
        created = parse(["new", "--name", "a", "--project", str(tmp_path),
                         "--service-tier", tier.value])
        assert created.service_tier is tier''')
f='tests/test_command_target_catalog.py'
for s in ('import asyncio',
          'from dataclasses import replace',
          'from agent_comms.cli_verbs.fork import ForkCliCommand',
          'from agent_comms.config_options import ServiceTierConfigOption',
          'from agent_comms.execution.pi_vocabulary import ServiceTier',
          'from agent_comms.projects.checkouts import LaunchCheckout',
          'from agent_comms.registry.thread_settings import ThreadSettings'):
    imp(f,s)
patch(f,'test_a_project_menu_offers_a_new_thread',
      '    assert [action.declaration for action in actions] == [NewThreadCliCommand]',
      '''    assert [action.declaration for action in actions] == [NewThreadCliCommand]
    action, = actions
    command, = action.bound
    tier_field, = (entry for entry in action.editable_fields
                   if entry.name == ServiceTierConfigOption.declared_field().name)
    assert tier_field.catalog is ServiceTierConfigOption
    prepared = asyncio.run(tier_field.prepare(comms.configuration_catalogs, command))
    assert tuple(choice.value for choice in prepared.offered()) == tuple(tier.value for tier in ServiceTier)
    for tier in ServiceTier:
        selected = replace(command, name=f"new-{tier.value}", service_tier=tier)
        spec = selected.spec_type.from_task(
            name=selected.name, project=LaunchCheckout(tmp_path),
            **ThreadSettings.field_values(selected))
        stored = comms.registry.declare(spec.declaration())
        read_back = comms.registry.require(stored.name)
        assert ThreadSettings.capture(read_back) == ThreadSettings.capture(selected)
        fork = ForkCliCommand.thread_binding(read_back)
        assert ThreadSettings.capture(fork) == ThreadSettings.capture(read_back)''')
Path('.verification/new-thread-configuration/control.plan.json').write_text(json.dumps({'recipes':[{'recipe_id':'exercise-original-settings-transports-and-creation-catalog','operations':ops}]},indent=2))
