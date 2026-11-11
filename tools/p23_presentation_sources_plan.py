"""Author the two remaining nominal presentation read contracts for NRA."""

import json

from p23_revision_owner_plan import ROOT, SRC, imported, patch, source, stage

published = SRC / "messaging/published_bus.py"
message_bus = SRC / "messaging/message_bus.py"
goals = SRC / "planning/goal_management.py"
presentation = SRC / "presentation.py"
publication_source = published.read_text()
class_start = publication_source.index("class PublishedBus[Writes](ABC):")
original_class = publication_source[class_start:]
activity_method = source(published, "PublishedBus.opened_activity")
resources = (
    "class BusResources:\n"
    "    \"\"\"The original live bus, read ledger and shared activity read.\"\"\"\n"
    "    __slots__ = ()\n\n"
    "    bus: Bus\n"
    "    reads: ReadLedger\n\n" + activity_method + "\n\n"
)
publication = original_class.replace(activity_method, "", 1).replace(
    "    bus: Bus\n    reads: ReadLedger\n", "", 1,
).replace("class PublishedBus[Writes](ABC):", "class PublishedBus[Writes](BusResources, ABC):", 1)
reader = '''"""The existing bus snapshot and unread-count operations presentation consumes."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager

from agent_comms.bus import BusSnapshot
from agent_comms.messaging.published_bus import BusResources
from agent_comms.messaging.read_basis import DisplayViewer
from agent_comms.messaging.read_selection import CapturedSelection


class BusReadSource(BusResources, ABC):
    __slots__ = ()

    @abstractmethod
    def snapshot(self, viewer: DisplayViewer) -> AbstractContextManager[tuple[CapturedSelection, BusSnapshot]]:
        ...

    @abstractmethod
    def pending_counts_all(self, names: Sequence[str]) -> Mapping[str, int]:
        ...
'''
snapshots = '''"""The owning goal snapshot and original wait rows presentation reads."""

from abc import ABC, abstractmethod
from collections.abc import Callable

from agent_comms.planning.goal_presentation import GoalSnapshot
from agent_comms.planning.goal_waits import GoalWait, GoalWaits
from agent_comms.planning.goals import GoalDeclaration
from agent_comms.registry.registry_document import RegistrySnapshot
from agent_comms.registry.threads import Thread


class GoalSnapshots(ABC):
    __slots__ = ()

    waits: GoalWaits

    @abstractmethod
    def snapshot_for(self, owner: Thread, snapshot: RegistrySnapshot,
                     wait: Callable[[GoalDeclaration], GoalWait]) -> GoalSnapshot:
        ...
'''
before = presentation.read_text()
after = before.replace(
    "if TYPE_CHECKING:\n    from agent_comms.messaging.message_bus import MessageBus\n"
    "    from agent_comms.planning.goal_management import Goals\n", "", 1,
).replace(": MessageBus", ": BusReadSource").replace(": Goals", ": GoalSnapshots")
if after == before or "TYPE_CHECKING:" in after:
    raise ValueError("Presentation contract source no longer matches the admitted two-edge cut")
regression = '''

def test_presentation_sources_are_actual_nominal_providers() -> None:
    from inspect import isabstract

    from agent_comms.messaging.message_bus import MessageBus
    from agent_comms.messaging.published_bus import PublishedBus
    from agent_comms.planning.goal_management import Goals
    from agent_comms.presentation import (
        BusPresentation, CoordinationSnapshot, CountedRead, InboxRead,
        ThreadRecord, ThreadView, UncountedRead,
    )

    bus_source = get_type_hints(BusPresentation.__init__)["bus"]
    goal_source = get_type_hints(BusPresentation.__init__)["goals"]
    assert bus_source in MessageBus.__bases__
    assert goal_source in Goals.__bases__
    assert not isabstract(MessageBus)
    assert not isabstract(Goals)
    assert bus_source.opened_activity is PublishedBus.opened_activity
    assert get_type_hints(goal_source.snapshot_for) == get_type_hints(Goals.snapshot_for)
    for callback in (InboxRead.counts, CountedRead.counts, UncountedRead.counts,
                     ThreadRecord.read, ThreadView.roster, CoordinationSnapshot.capture):
        get_type_hints(callback)
'''
# Keep imports at module scope; the hidden-source control must still import only original types.
imports, regression_body = regression.split("    bus_source =", 1)
regression = "\n\ndef test_presentation_sources_are_actual_nominal_providers() -> None:\n    bus_source =" + regression_body
runtime_test = ROOT / "tests/test_revision_owner_annotations.py"
original_test = runtime_test.read_text()
updated_test = original_test.replace("from typing import get_type_hints", "from typing import get_type_hints\nfrom inspect import isabstract", 1).replace(
    "from agent_comms.history_views import HistoryViews",
    "from agent_comms.history_views import HistoryViews\n"
    "from agent_comms.messaging.message_bus import MessageBus\n"
    "from agent_comms.messaging.published_bus import PublishedBus\n"
    "from agent_comms.planning.goal_management import Goals", 1,
).replace(
    "from agent_comms.presentation import WireRevision, expiry_period_changed, revision_source",
    "from agent_comms.presentation import (\n"
    "    BusPresentation, CoordinationSnapshot, CountedRead, InboxRead,\n"
    "    ThreadRecord, ThreadView, UncountedRead, WireRevision, expiry_period_changed, revision_source,\n)", 1,
) + regression
print(json.dumps({"stages": [
    stage("p23-presentation-read-contract-declarations",
          "Admitted operations: presentation reads the original bus resources, opened_activity, snapshot and pending_counts_all; it reads Goals.waits and Goals.snapshot_for without mutation. Keep concrete captured selections, BusSnapshots, Thread and GoalSnapshot identities. Declare state-free nominal contracts below the actual implementations, not structural Protocols, adapters, stored mirrors or forwarding methods.", [
              {"operation": "create_file", "file_path": str(SRC / "messaging/bus_read_source.py"), "source": reader},
              {"operation": "create_file", "file_path": str(SRC / "goal_snapshots.py"), "source": snapshots},
              patch(published, [(original_class, resources + publication)]),
          ]),
    stage("p23-original-presentation-providers-inherit-their-read-contracts",
          "BusResources owns the original bus/reads declarations and exact opened_activity body once. PublishedBus and BusReadSource share that provider through MI; MessageBus retains its concrete snapshot/count methods and original constructor. Goals retains the exact owning snapshot implementation. Prove C3, concreteness, field annotations, held-capture lifetime and original consumers at runtime; no automatic equivalence claim follows from source simulation.", [
              imported(message_bus, "from agent_comms.messaging.bus_read_source import BusReadSource"),
              patch(message_bus, [("class MessageBus[Writes: Publisher](PublishedBus[Writes]):", "class MessageBus[Writes: Publisher](PublishedBus[Writes], BusReadSource):")]),
              imported(goals, "from agent_comms.planning.goal_snapshots import GoalSnapshots"),
              patch(goals, [("    GoalStoreSource[GoalAttemptStore], GoalReadyGrantSource[NativeThread, Generation],", "    GoalStoreSource[GoalAttemptStore], GoalReadyGrantSource[NativeThread, Generation], GoalSnapshots,")]),
          ]),
    stage("p23-presentation-runtime-binding-and-original-provider-proof",
          "Bind all presentation annotations to the actual lower nominal read contracts, remove the remaining TYPE_CHECKING block, and resolve original callbacks without injected globals. This changes no constructor argument values, snapshot ownership, waits, scheduler control, backend observation or GoalExecution publication. The unchanged new case must fail with this source hidden, then pass restored; original bus, display, goal and native consumers remain required.", [
              patch(presentation, [(before, after)]),
              {"operation": "remove_import_names", "file_path": str(presentation), "module_name": "typing", "import_names": ["TYPE_CHECKING"]},
              imported(presentation, "from agent_comms.messaging.bus_read_source import BusReadSource"),
              imported(presentation, "from agent_comms.planning.goal_snapshots import GoalSnapshots"),
              patch(runtime_test, [(original_test, updated_test)]),
          ]),
]}, indent=2))
