"""Declare the original goal generation, attempt and context providers through NRA."""

import json

from p23_revision_owner_plan import ROOT, SRC, patch, stage

contracts = SRC / "goal_mutation.py"
ledger = SRC / "goal_ledger.py"
context = SRC / "execution/turn_context.py"
test = ROOT / "tests/test_goal_nominal_providers.py"

regression = '''"""Original providers own the admitted goal read contracts and their concrete results."""

from inspect import isabstract
from typing import get_args, get_origin, get_type_hints

from agent_comms.execution.context_owner import ContextOwner
from agent_comms.execution.goal_turn_permit import GoalTurnPermit
from agent_comms.execution.turn_context import TurnContext
from agent_comms.planning.goal_ledger import Generation, LedgerAttempt, Reservation
from agent_comms.planning.goal_mutation import GoalContextSource, GoalGenerationRead, GoalStoppedAttempt
from agent_comms.planning.goal_work import GoalWorkSource
from agent_comms.nominal.field_codec import GoalId
from agent_comms.planning.goal_attempt_identity import GoalAttemptIdentity


def test_original_generation_declares_its_read_contract_without_changing_fields():
    assert GoalGenerationRead in Generation.__bases__
    assert not isabstract(Generation)
    assert get_type_hints(Generation)["goal_id"] is GoalId
    assert get_type_hints(Generation)["number"] is int
    assert get_type_hints(Generation.attempt_identity.fget)["return"] is GoalAttemptIdentity


def test_original_ledger_family_binds_the_concrete_reservation():
    assert GoalStoppedAttempt in LedgerAttempt.__bases__
    alias, = (base for base in LedgerAttempt.__orig_bases__
              if get_origin(base) is GoalStoppedAttempt)
    assert get_args(alias) == (Reservation,)
    assert get_type_hints(LedgerAttempt.stopped_permit)["return"] is GoalTurnPermit
    for member in LedgerAttempt.members():
        assert issubclass(member, GoalStoppedAttempt)
        assert not isabstract(member)


def test_original_turn_context_declares_its_owner_and_goal_inputs():
    assert GoalContextSource in TurnContext.__bases__
    assert not isabstract(TurnContext)
    alias, = (base for base in TurnContext.__orig_bases__
              if get_origin(base) is GoalContextSource)
    assert get_args(alias)[:2] == (ContextOwner, GoalWorkSource)
    hints = get_type_hints(TurnContext.with_goal)
    assert hints["owner"] is ContextOwner
    assert hints["goals"] is GoalWorkSource
    assert hints["return"] is TurnContext
'''

print(json.dumps({"stages": [stage(
    "p23-original-goal-providers-own-nominal-read-membership",
    "P23 requires actual nominal providers, not structural Protocol substitutes. The complete source trace finds Generation as the concrete generation reader, LedgerAttempt and its declared family as the stopped-permit providers, and TurnContext as the three-argument goal-context producer; TaskScope.with_goal is a different one-argument operation and is forbidden from this membership. Keep concrete Generation fields and its attempt_identity implementation; plain read-contract attribute annotations avoid shadowing frozen dataclass fields with inherited abstract properties. Bind LedgerAttempt to its already-defined concrete Reservation, not a generic persisted record. Bind the original TurnContext to ContextOwner, GoalWorkSource and its own concrete result. The GoalWorkSource input covers exactly GoalSegment.capture's original order.todos/instructions/progress reads. Add only state-free ABC bases and abstract signatures; all producer, refusal, acquisition and persistence bodies are unchanged. Exact source patches are native NRA transactions, not automatic behavioral proofs; baseline failures, runtime hints, C3/concreteness and original goal/context consumers remain required.",
    [
        patch(contracts, [
            ("from typing import Protocol\n", ""),
            ("class GoalGenerationRead(Protocol):\n    @property\n    def goal_id(self) -> GoalId: ...\n\n    @property\n    def number(self) -> int: ...\n\n    @property\n    def attempt_identity(self) -> GoalAttemptIdentity: ...",
             "class GoalGenerationRead(ABC):\n    __slots__ = ()\n\n    goal_id: GoalId\n    number: int\n\n    @property\n    @abstractmethod\n    def attempt_identity(self) -> GoalAttemptIdentity: ..."),
            ("class GoalStoppedAttempt[Reservation](Protocol):\n    def stopped_permit(",
             "class GoalStoppedAttempt[Reservation](ABC):\n    __slots__ = ()\n\n    @abstractmethod\n    def stopped_permit("),
            ("class GoalContextSource[Owner, Goals, Context](Protocol):\n    def with_goal(self, owner: Owner, goals: Goals) -> Context: ...",
             "class GoalContextSource[Owner, Goals, Context](ABC):\n    __slots__ = ()\n\n    @abstractmethod\n    def with_goal(self, owner: Owner, goals: Goals) -> Context: ..."),
        ]),
        patch(ledger, [
            ("from agent_comms.planning.goal_mutation import GoalFailureProjection",
             "from agent_comms.planning.goal_mutation import GoalFailureProjection, GoalGenerationRead, GoalStoppedAttempt"),
            ("class Generation(GoalLedgerTable, TypedTable):", "class Generation(GoalLedgerTable, TypedTable, GoalGenerationRead):"),
            ("class LedgerAttempt(DeclaredFamily, affix=\"Attempt\"):",
             "class LedgerAttempt(DeclaredFamily, GoalStoppedAttempt[Reservation], affix=\"Attempt\"):"),
        ]),
        patch(context, [
            ("from agent_comms.instruction_bytes import InstructionBytes",
             "from agent_comms.planning.goal_mutation import GoalContextSource\nfrom agent_comms.planning.goal_work import GoalWorkSource\nfrom agent_comms.instruction_bytes import InstructionBytes"),
            ("class TurnContext(DeclaredReply):",
             "class TurnContext(DeclaredReply, GoalContextSource[ContextOwner, GoalWorkSource, \"TurnContext\"]):"),
            ("def with_goal(self, owner: ContextOwner, goals) -> TurnContext:",
             "def with_goal(self, owner: ContextOwner, goals: GoalWorkSource) -> TurnContext:"),
            ("def prepend_goal(self, owner: ContextOwner, goal: Goal, goals) -> TurnContext:",
             "def prepend_goal(self, owner: ContextOwner, goal: Goal, goals: GoalWorkSource) -> TurnContext:"),
            ("def capture(cls, owner: ContextOwner, goal: Goal, goals) -> GoalSegment:",
             "def capture(cls, owner: ContextOwner, goal: Goal, goals: GoalWorkSource) -> GoalSegment:"),
        ]),
        {"operation": "create_file", "file_path": str(test), "source": regression},
    ],
)]}, indent=2))
