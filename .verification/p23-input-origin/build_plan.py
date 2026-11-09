"""Build the assigned InputOrigin contribution with existing native operations."""

import json
from pathlib import Path

SOURCE = Path('/home/ts/wt/comms-friction--P17/src/agent_comms')
ARTIFACTS = Path(__file__).parent


def patch(module: str, *changes: tuple[str, str]) -> dict:
    return {
        'operation': 'patch_target',
        'file_path': str(SOURCE / module),
        'replacements': [
            {'old_source': before, 'new_source': after} for before, after in changes
        ],
    }


def imports(module: str, source: str) -> dict:
    return {'operation': 'ensure_import', 'file_path': str(SOURCE / module),
            'import_source': source}


def add_base(module: str, target: str, base: str) -> dict:
    return {'operation': 'add_class_base', 'file_path': str(SOURCE / module),
            'target_qualname': target, 'base_name': base}


def stage(name: str, reason: str, operations: list[dict]) -> dict:
    return {'recipes': [{'recipe_id': name, 'reason': reason,
                         'operations': operations, 'architecture_guards': [],
                         'authority_claims': []}], 'architecture_guards': []}


lookup = {
    'operation': 'relocate_symbols_to_new_module',
    'file_path': str(SOURCE / 'registry/registry_document.py'),
    'symbol_qualnames': ['UserThreadLookup', 'FoundUserThread', 'NoUserThread'],
    'destination_path': str(SOURCE / 'registry/user_thread_lookup.py'),
}

operations = [{
    'operation': 'create_file', 'file_path': str(SOURCE / 'input_ingress.py'),
    'source': (ARTIFACTS / 'input_ingress.py.txt').read_text(),
}]

operations.extend([
    {'operation': 'replace_class_base',
     'file_path': str(SOURCE / 'private_registry_guard.py'),
     'target_qualname': 'RegistryPrivacy', 'base_name': 'ABC',
     'replacement_base_name': 'InputRootAuthority'},
    patch('private_registry_guard.py',
          ('    @property\n    @abstractmethod\n    def namespace(self) -> RootId:\n        """Require the original native namespace; a public registry has none."""\n\n', ''),
          ('    @abstractmethod\n    def require_root(self, root_id: RootId, error: Exception) -> None: ...\n\n', '')),
    imports('private_registry_guard.py', 'from agent_comms.input_ingress import InputRootAuthority'),
    add_base('registry/registry_document.py', 'RegistrySnapshot', 'InputRegistry[Thread]'),
    imports('registry/registry_document.py', 'from agent_comms.input_ingress import InputRegistry'),
    add_base('registry/registry_store.py', 'RegistryCut', 'InputAuthorityCut[Thread]'),
    add_base('registry/registry_store.py', 'RegistryStore', 'InputRegistryStore[Thread]'),
    imports('registry/registry_store.py', 'from agent_comms.input_ingress import InputAuthorityCut, InputRegistryStore'),
    imports('registry/registry_store.py', 'from agent_comms.registry.threads import Thread'),
    add_base('registry/registration.py', 'Registration', 'InputRegistration[Thread]'),
    imports('registry/registration.py', 'from agent_comms.input_ingress import InputRegistration'),
    imports('registry/registration.py', 'from agent_comms.registry.threads import Thread'),
    add_base('messaging/sending.py', 'Messaging', 'InputUserIdentity[Thread]'),
    imports('messaging/sending.py', 'from agent_comms.input_ingress import InputUserIdentity'),
    patch('comms.py',
          ('class Comms(ActivityClient, ContextAssembly):',
           'class Comms(ActivityClient, ContextAssembly, InputIngressContext[Thread],\n            InputTextContext[InputAttempt]):\n    input_dispositions: ClassVar[Callable[[Path], InputLookup[InputAttempt]]] = InputDispositions\n')),
    imports('comms.py', 'from agent_comms.input_ingress import InputIngressContext, InputTextContext'),
    imports('comms.py', 'from agent_comms.input_attempt import InputAttempt'),
    imports('comms.py', 'from agent_comms.input_disposition import InputDispositions'),
    imports('comms.py', 'from agent_comms.input_lookup import InputLookup'),
    imports('comms.py', 'from agent_comms.registry.threads import Thread'),
    imports('comms.py', 'from collections.abc import Callable'),
])

old_guard = '''if TYPE_CHECKING:
    from agent_comms.comms import Comms
    from agent_comms.input_attempt import StoredInput
    from agent_comms.registry.registry_document import RegistrySnapshot
    from agent_comms.registry.threads import Thread
'''

operations.extend([
    patch('input_origin.py',
          ('from typing import TYPE_CHECKING, Annotated', 'from typing import Annotated'),
          (old_guard, ''),
          ('    def require_ingress(self, comms: Comms, snapshot: RegistrySnapshot,\n                        claim_scope: ClaimedIdentity, controller) -> None:',
           '    def require_ingress[Owner: TaskOwner](\n        self, comms: InputIngressContext[Owner], snapshot: InputRegistry[Owner],\n        claim_scope: ClaimedIdentity, controller,\n    ) -> None:'),
          ('    def retained_fact(self, source: StoredInput):\n        from agent_comms.retained_task_facts import InputTaskFact\n\n        return InputTaskFact(source)',
           '    def retained_fact(self, source: input_attempt.StoredInput):\n        return task_facts.InputTaskFact(source)'),
          ('    def public_text(self, comms) -> str:\n        from agent_comms.input_disposition import InputDispositions\n\n        return self.require_original(InputDispositions(comms.root)).source_text',
           '    def public_text(self, comms: InputTextContext[input_attempt.InputAttempt]) -> str:\n        return self.require_original(comms.input_dispositions(comms.root)).source_text'),
          ('    def require_original(self, inputs):',
           '    def require_original(self, inputs: InputLookup[input_attempt.InputAttempt]) -> input_attempt.StoredInput:'),
          ('    def capture(cls, comms: Comms, claim_scope: ClaimedIdentity) -> HumanInputOrigin:',
           '    def capture[Owner: TaskOwner](\n        cls, comms: InputIngressContext[Owner], claim_scope: ClaimedIdentity,\n    ) -> HumanInputOrigin:'),
          ('    def _require_owner(snapshot: RegistrySnapshot, claim_scope: ClaimedIdentity) -> Thread:',
           '    def _require_owner[Owner: TaskOwner](\n        snapshot: InputRegistry[Owner], claim_scope: ClaimedIdentity,\n    ) -> Owner:'),
          ('    def retained_fact(self, source):\n        from agent_comms.retained_task_facts import HumanInputTaskFact\n\n        return HumanInputTaskFact(source)',
           '    def retained_fact(self, source: input_attempt.StoredInput):\n        return task_facts.HumanInputTaskFact(source)'),
          ('    def applies(self, owner: Thread, snapshot: RegistrySnapshot) -> bool:',
           '    def applies(self, owner: TaskOwner, snapshot: TaskRegistry[TaskOwner]) -> bool:')),
    imports('input_origin.py', 'from agent_comms.input_ingress import InputIngressContext, InputRegistry, InputTextContext'),
    imports('input_origin.py', 'from agent_comms.input_lookup import InputLookup'),
    imports('input_origin.py', 'from agent_comms.task_owner import TaskOwner'),
    imports('input_origin.py', 'from agent_comms.task_registry import TaskRegistry'),
])

# These ordinary module bindings follow the declarations. Original postponed
# concrete field annotations remain concrete, including during circular imports.
origin = (SOURCE / 'input_origin.py').read_text()
last = origin.rstrip().splitlines()[-1]
operations.append(patch('input_origin.py', (
    last,
    last + '\n\n\nimport agent_comms.input_attempt as input_attempt\nimport agent_comms.retained_task_facts as task_facts',
)))

reason = (
    'Preserve the original InputOrigin family and wire fields. Actual RegistrySnapshot, '
    'RegistryCut, RegistryStore, Registration, Messaging, Comms and RegistryPrivacy '
    'inherit state-free contracts; no provider body, storage field or refusal policy '
    'is copied. Capture keeps both snapshot_with_authority calls around user_identity '
    'and the root/claim rechecks unchanged. Public text constructs the actual '
    'InputDispositions at the same call site and time, using its original constructor '
    'bound on Comms, with no new forwarding function or cache. Relocate the original '
    'UserThreadLookup family and migrate consumers with the native operation. '
    'DEPENDENCY: compose after friction InputLookup and InitialOrigin relocations, '
    'and after ordinary module-qualified original Message/StoredInput fact bindings '
    'in retained_task_facts; its direct from-input_attempt import cannot remain when '
    'InputOrigin binds task_facts. This contribution is not qualification or a final '
    'delivery claim; run combined cold/concrete-field/ingress consumers before acceptance.'
)

publication = [
    {'operation': 'create_file',
     'file_path': str(SOURCE / 'input_publication_read.py'),
     'source': (ARTIFACTS / 'input_publication_read.py.txt').read_text()},
    {'operation': 'create_file',
     'file_path': str(SOURCE / 'input_publication_source.py'),
     'source': (ARTIFACTS / 'input_publication_source.py.txt').read_text()},
    add_base('bus.py', 'BusReader', 'InputPublicationSource'),
    imports('bus.py', 'from agent_comms.input_publication_source import InputPublicationSource'),
    patch('task_sources.py',
          ('    def require_wording_publication(self, registry: TaskRegistry[TaskOwner], original_source: BusReader) -> None:\n        from agent_comms.input_disposition import InputDispositions\n        from agent_comms.registry.registry_store import RegistryStore\n\n        privacy = RegistryStore.at(original_source.path.parent).privacy()\n        row = self.subject.require_human_input().require_original(\n            InputDispositions(original_source.path.parent))',
           '    def require_wording_publication(self, registry: TaskRegistry[TaskOwner], original_source: InputPublicationRead[Provenance, input_attempt.StoredInput]) -> None:\n        privacy, row = original_source.input_publication(self.subject)')),
    imports('task_sources.py', 'from agent_comms.input_publication_read import InputPublicationRead'),
]
publication_reason = (
    'Task bootstrap imports only the low generic InputPublicationRead API. The actual '
    'BusReader inherits InputPublicationSource, which inherits that API with original '
    'Provenance and StoredInput types. Store imports execute only on this implementation '
    'side, not through task_sources. Original NativeInputConstraintPin I/O is inherited '
    'by the actual root-bearing BusReader. It acquires original RegistryStore.privacy(), then looks up and checks '
    'original input provenance using actual InputDispositions. It returns the same '
    'privacy object and StoredInput; no namespace value or surrogate record is '
    'captured. origin.require_human and privacy.namespace remain in the caller '
    'after that read, preserving PublicRegistry namespace refusal order and all '
    'author/recipient/project checks. This stage follows friction message-read '
    'provider headers and the InputOrigin strong contracts. Concrete callback '
    'annotations use ordinary original-module bindings after provider declaration.'
)
plan = {'stages': [stage('p23-original-user-selection-family', reason, [lookup]),
                   stage('p23-input-origin-strong-acquisition', reason, operations),
                   stage('p23-native-input-publication-read', publication_reason, publication)]}
(ARTIFACTS / 'input-origin-v3.plan.json').write_text(json.dumps(plan, indent=2) + '\n')
print(f'{len(plan["stages"])} stages; {1 + len(operations) + len(publication)} operations; unqualified')
