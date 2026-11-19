import json
from pathlib import Path
root = Path.cwd()
ops = []
def patch(file, target, old, new):
    ops.append(dict(operation='patch_target', file_path=str(root/file), target_qualname=target,
                    replacements=[dict(old_source=old, new_source=new)]))
def imp(file, source):
    ops.append(dict(operation='ensure_import', file_path=str(root/file), import_source=source))
f = 'src/agent_comms/registry/thread_settings.py'
imp(f, 'from agent_comms.nominal.field_codec import Catalog')
patch(f, 'ConfigurationSlot', 'class ConfigurationSlot(ValueDirective[type]):', 'class ConfigurationSlot(Catalog):')
f = 'src/agent_comms/registry/native_settings.py'
imp(f, 'from agent_comms.registry.thread_settings import ThreadSettings')
patch(f, 'NativeThreadDefaults', 'class NativeThreadDefaults(JsonFlagRecord):', 'class NativeThreadDefaults(ThreadSettings, JsonFlagRecord):')
f = 'src/agent_comms/cli_verbs/new.py'
imp(f, 'from agent_comms.registry.thread_settings import ThreadSettings')
patch(f, 'NewThreadCliCommand', '    CreatesThread, NameArgument, CatalogSelection, TargetActionCommand[ThreadCreatedResult],',
      '    ThreadSettings, CreatesThread, NameArgument, CatalogSelection, TargetActionCommand[ThreadCreatedResult],')
patch(f, 'NewThreadCliCommand', '''    model: Annotated[
        ThreadModel, Catalog(ModelSetting), Argument(flags=("--model",)),
    ] = NativeThreadDefaults.model
    thinking_level: Annotated[
        ThinkingLevelField,
        Catalog(ThinkingSetting),
        Argument(flags=("--thinking-level",)),
    ] = NativeThreadDefaults.thinking_level''', '''    model: FieldCodec.field_annotation(ThreadSettings, "model") = NativeThreadDefaults.model
    thinking_level: FieldCodec.field_annotation(ThreadSettings, "thinking_level") = NativeThreadDefaults.thinking_level''')
patch(f, 'NewThreadCliCommand.project_bindings', '''        return (cls(name='', project=project, model=defaults.model,
                    thinking_level=defaults.thinking_level),)''', '''        return (cls(name='', project=project, **NativeThreadDefaults.field_values(defaults)),)''')
f = 'src/agent_comms/registry/thread_management.py'
imp(f, 'from agent_comms.registry.thread_settings import ThreadSettings')
patch(f, 'NewThreadSpec', 'class NewThreadSpec:', 'class NewThreadSpec(ThreadSettings):')
patch(f, 'NewThreadSpec', '''    model: ThreadModel
    thinking_level: ThinkingLevelField''', '''    model: ThreadModel = field()
    thinking_level: ThinkingLevelField = field()''')
patch(f, 'NewThreadSpec.declaration', '''                      worktree=self.project, model=self.model, thinking_level=self.thinking_level)''',
      '''                      worktree=self.project, **ThreadSettings.field_values(self))''')
f = 'src/agent_comms/cli_verbs/fork.py'
imp(f, 'from agent_comms.registry.thread_settings import ThreadSettings')
patch(f, 'ForkCliCommand.thread_binding', '                   model=thread.model, thinking_level=thread.thinking_level)',
      '                   **ThreadSettings.field_values(thread))')
f = 'src/agent_comms/planning/work_dispatch.py'
patch(f, 'WorkDispatch.start', '''            model=defaults.model,
            thinking_level=defaults.thinking_level,''', '''            **NativeThreadDefaults.field_values(defaults),''')
Path('.verification/new-thread-configuration/fix.plan.json').write_text(json.dumps({
    'recipes': [{'recipe_id': 'inherit-complete-thread-configuration-on-creation', 'operations': ops}]}, indent=2))
ops = []
f = 'src/agent_comms/registry/thread_model.py'
patch(f, 'CatalogSelection', '''    """The model and thinking inputs a catalog reads; absent inputs select no override."""''',
      '''    """The settings and project supplied by the selection's actual owner."""''')
patch(f, 'CatalogSelection', '''    @property
    def model(self) -> ModelSelection:
        return UndeclaredModel()

    @property
    def thinking_level(self) -> type[ThinkingChoice]:
        return NoThinkingOverride

    @property
    def service_tier(self) -> ServiceTier:
        return ServiceTier.default()''', '''    model: ThreadModel
    thinking_level: type[ThinkingChoice]
    service_tier: ServiceTier''')
for file, names in (
    ('src/agent_comms/registry/thread_settings.py', ['ValueDirective']),
    ('src/agent_comms/registry/thread_model.py', ['NoThinkingOverride']),
    ('src/agent_comms/cli_verbs/new.py', ['ThinkingLevelField', 'Catalog', 'ThreadModel', 'ModelSetting', 'ThinkingSetting']),
):
    ops.append(dict(operation='remove_import_bindings', file_path=str(root/file), bound_names=names))
Path('.verification/new-thread-configuration/selection.plan.json').write_text(json.dumps({
    'recipes': [{'recipe_id': 'remove-competing-catalog-selection-defaults', 'operations': ops}]}, indent=2))
