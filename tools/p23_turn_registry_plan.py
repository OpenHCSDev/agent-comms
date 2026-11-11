"""Build the exact P23 turn-contract trajectory; NRA owns application."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path('/home/ts/.cache/agent-scratch/comms-friction/p23-turn-registry-plan.json')
ops = []

def patch(file, old, new, target=None):
    op = {'operation': 'patch_target', 'file_path': str(ROOT / file), 'replacements': [{'old_source': old, 'new_source': new}]}
    if target:
        op['target_qualname'] = target
    ops.append(op)

source = '''"""The existing registry constructor and turn transactions, below turn values."""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path


class TurnRegistry[Record, Declaration, Locked](ABC):
    path: Path

    @abstractmethod
    def __init__(self, path: Path) -> None: ...

    @abstractmethod
    def read_turn(self, held: Record) -> Declaration: ...

    @abstractmethod
    def change_turn[Result](
        self, record: Record, change: Callable[[Locked], Result],
    ) -> Result: ...
'''
ops.append({'operation': 'create_file', 'file_path': str(ROOT / 'src/agent_comms/turn_registry.py'), 'source': source})
lease = 'src/agent_comms/turn_lease.py'
patch(lease, 'from typing import TYPE_CHECKING, Annotated, ClassVar', 'from typing import Annotated, ClassVar')
for line in ('from .coordination.session import NativeCapture\n', 'from .input_disposition import InputDispositions\n', 'from .message_reference import MessageReference\n'):
    patch(lease, line, '')
patch(lease, 'from .turn_phase import IdlePhase, PreparingPhase, TurnPhase\n', 'from .turn_phase import IdlePhase, PreparingPhase, TurnPhase\nfrom .turn_registry import TurnRegistry\n')
patch(lease, 'if TYPE_CHECKING:\n    from .messaging import Messaging\n    from .registry.registry_document import RegistrySnapshot\n    from .registry.registry_store import RegistryStore\n    from .registry.threads import Thread\n', '')
for owner in ('TurnLease', 'NoTurnLease', 'TurnLeaseFence'):
    ops.append({'operation': 'delete_target', 'file_path': str(ROOT / lease), 'target_qualname': owner + '.answered_inputs'})
factory = 'type[TurnRegistry[StoredTurnRecord, TurnDeclaration, LockedTurn]]'
for owner, result in [('TurnRecord', 'TurnRecord'), ('NoTurnRecord', 'NoTurnRecord'), ('FinishedTurn', 'FinishedTurn'), ('StoredTurnReference', 'StoredTurnRecord'), ('StoredTurnRecord', 'StoredTurnRecord')]:
    patch(lease, f'def bound_to(self, registry_path: Path, identity: TurnIdentity) -> {result}:', f'def bound_to(\n        self, registry_path: Path, identity: TurnIdentity, *,\n        registry_type: {factory},\n    ) -> {result}:', owner + '.bound_to')
patch(lease, 'return StoredTurnRecord.for_identity(registry_path, identity)', 'return StoredTurnRecord.for_identity(registry_path, identity, registry_type=registry_type)', 'StoredTurnReference.bound_to')
patch(lease, '    @property\n    def path(self) -> Path:', f'    registry_type: Annotated[{factory}, WireTransient()] = field(\n        repr=False, compare=False\n    )\n\n    @property\n    def path(self) -> Path:', 'StoredTurnRecord')
patch(lease, 'def registry(self) -> RegistryStore:\n        """The registry owner this stored reference is bound to."""\n        from .registry.registry_store import RegistryStore\n\n        return RegistryStore(self.registry_path)', 'def registry(self) -> TurnRegistry[StoredTurnRecord, TurnDeclaration, LockedTurn]:\n        """Construct the original registry provider bound by its declaration."""\n        return self.registry_type(self.registry_path)', 'StoredTurnRecord.registry')
patch(lease, 'record = super().bound_to(registry_path, identity)\n        return (self if (self.registry_path, self.identity) ==\n                (record.registry_path, record.identity) else record)', 'record = super().bound_to(registry_path, identity, registry_type=registry_type)\n        return (self if (self.registry_path, self.identity, self.registry_type) ==\n                (record.registry_path, record.identity, record.registry_type) else record)', 'StoredTurnRecord.bound_to')
patch(lease, 'def for_identity(cls, registry_path: Path, identity: TurnIdentity) -> StoredTurnRecord:\n        return cls(identity, registry_path.resolve())', f'def for_identity(\n        cls, registry_path: Path, identity: TurnIdentity, *,\n        registry_type: {factory},\n    ) -> StoredTurnRecord:\n        return cls(identity, registry_path.resolve(), registry_type)', 'StoredTurnRecord.for_identity')
patch(lease, 'cls, registry_path: Path, identity: TurnIdentity, turn: ActiveTurn,\n', f'cls, registry_path: Path, identity: TurnIdentity, turn: ActiveTurn, *,\n        registry_type: {factory},\n', 'StoredTurnRecord.begin')
patch(lease, 'record = cls.for_identity(registry_path, identity)', 'record = cls.for_identity(registry_path, identity, registry_type=registry_type)', 'StoredTurnRecord.begin')
for owner in ('TurnCompletion', 'NoFinishedTurn', 'FinishedTurnFence'):
    patch(lease, 'def idle_sources(self, snapshot: RegistrySnapshot) -> tuple[Thread, ...]:', 'def idle_sources[Source](\n        self, idle_after: Callable[[TurnFence], tuple[Source, ...]],\n    ) -> tuple[Source, ...]:', owner + '.idle_sources')
patch(lease, 'return snapshot.idle_after(self)', 'return idle_after(self)', 'FinishedTurnFence.idle_sources')
store = 'src/agent_comms/registry/registry_store.py'
patch(store, 'from ..routing import Routing\n', 'from ..routing import Routing\nfrom ..turn_registry import TurnRegistry\n')
patch(store, 'class RegistryStore(RegistryLocking, RegistryFile, CarriedDocument[RegistryDocument]):', 'class RegistryStore(RegistryLocking, RegistryFile, CarriedDocument[RegistryDocument],\n                    TurnRegistry[StoredTurnRecord, TurnDeclaration, LockedTurn]):')
patch(store, 'StoredTurnRecord.begin(self.store.path, identity, turn)', 'StoredTurnRecord.begin(self.store.path, identity, turn, registry_type=RegistryStore)')
patch(store, 'StoredTurnRecord.for_identity(self.store.path, current.turn_identity)', 'StoredTurnRecord.for_identity(self.store.path, current.turn_identity, registry_type=RegistryStore)')
patch(store, 'thread.turn_record.bound_to(self.path, thread.turn_identity)', 'thread.turn_record.bound_to(self.path, thread.turn_identity, registry_type=RegistryStore)')
messaging = 'src/agent_comms/messaging.py'
patch(messaging, 'from .external_transcript import ExternalTranscript\n', 'from .external_transcript import ExternalTranscript\nfrom .input_disposition import InputDispositions\n')
patch(messaging, '        references = list(owner.turn_lease.answered_inputs(self, owner, target))', '''        references: list[MessageReference] = []
        for lease in owner.turn_lease.held():
            inputs = InputDispositions(self.root).in_turn(lease.turn_id)
            with NativeCapture(self.bus) as boundary, self.bus.bus.reading() as source:
                references.extend(inputs.answered_by(
                    lease, source, boundary.registry, target, privacy=boundary.privacy,
                ))''', 'Messaging._answered_by')
recovery = 'src/agent_comms/planning/goal_wait_recovery.py'
patch(recovery, 'fence.idle_sources(snapshot)', 'fence.idle_sources(snapshot.idle_after)')
patch(recovery, 'fence.idle_sources(current)', 'fence.idle_sources(current.idle_after)')
patch('tests/test_goal_failure_observation.py', 'from agent_comms.turn_lease import FinishedTurn, StoredTurnRecord, TurnLeaseFence\n', 'from agent_comms.turn_lease import FinishedTurn, StoredTurnRecord, TurnLeaseFence\nfrom agent_comms.registry.registry_store import RegistryStore\n')
patch('tests/test_goal_failure_observation.py', 'StoredTurnRecord.for_identity(thread_registration.store.path, identity)', 'StoredTurnRecord.for_identity(thread_registration.store.path, identity, registry_type=RegistryStore)')
patch('tests/test_turn_identity_equality.py', 'from agent_comms.turn_lease import FinishedTurnFence, StoredTurnRecord, TurnLeaseFence\n', 'from agent_comms.turn_lease import FinishedTurnFence, StoredTurnRecord, TurnLeaseFence\nfrom agent_comms.registry.registry_store import RegistryStore\n')
patch('tests/test_turn_identity_equality.py', 'StoredTurnRecord.for_identity(tmp_path / "registry.json", identity)', 'StoredTurnRecord.for_identity(tmp_path / "registry.json", identity, registry_type=RegistryStore)')
patch('tests/test_cli.py', 'from agent_comms.turn_lease import ActiveTurn, RecordedGeneration, StoredTurnRecord\n', 'from agent_comms.turn_lease import ActiveTurn, RecordedGeneration, StoredTurnRecord\nfrom agent_comms.registry.registry_store import RegistryStore\n')
patch('tests/test_cli.py', '            comms.registry.store.path,\n            busy,', '            comms.registry.store.path,\n            busy,')
# Preserve the existing begin call's positional fields and add only its constructor binding.
patch('tests/test_cli.py', '                turn_generation=RecordedGeneration(busy.generation),\n            ),\n        )', '                turn_generation=RecordedGeneration(busy.generation),\n            ),\n            registry_type=RegistryStore,\n        )')
# A no-change patch is not an operation.
ops = [op for op in ops if op['operation'] != 'patch_target' or op['replacements'][0]['old_source'] != op['replacements'][0]['new_source']]
plan = {'recipes': [{'recipe_id': 'p23-turn-registry-original-constructor', 'reason': 'Preserve the original RegistryStore constructor, normalized registry path, transient wire exclusions, exact fences, lock/cancel ordering, current snapshot and callbacks. RegistryStore supplies its original class at the existing binding points; turn values consume the lower generic transaction contract. Messaging consumes the existing held fence rather than duplicate lease-presence dispatch. Terminal wait recovery passes the same captured snapshot idle_after method, not a replacement snapshot or cached status. Authored semantic boundary checked by runtime annotations and real registry/input/wait consumers; exact patch preflight alone is not equivalence proof. Remaining routing/messages dependency is not claimed removed.', 'operations': ops, 'architecture_guards': [], 'authority_claims': []}], 'architecture_guards': []}
OUT.write_text(json.dumps(plan, indent=2) + '\n')
print(OUT)
