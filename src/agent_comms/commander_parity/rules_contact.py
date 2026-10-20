"""Rules/contact comparisons use the existing installed-command observation lifetime."""

from pathlib import Path
from tempfile import TemporaryDirectory

from ..commander.config import CommanderConfig
from ..committed_store_copy import CommittedStoreCopy
from ..comms import Comms
from ..launch_runtime import CallingThreadUnavailable, ThreadLaunch
from ..maintenance_barrier import MaintenanceBarrier
from ..store_files import _store_lock
from . import ParityComparison, UnverifiedParity
from .load import CommandComparison
from .observations import (
    OrderContactObservation,
    RuleReviewObservation,
    StandingRulesObservation,
)


class StandingRulesComparison(CommandComparison):
    arguments = StandingRulesObservation.arguments()
    timeout = 120

    @classmethod
    def read(cls) -> ParityComparison:
        config = CommanderConfig.read(CommanderConfig.path())
        comms = Comms(config.root)
        try:
            caller = ThreadLaunch.current().name
        except CallingThreadUnavailable:
            caller = config.commander
        owner = comms.registry.require(caller)
        storage = comms.registry.storage_rule
        size = storage.tree_gib(config.root)
        occupied = storage.tree_gib(owner.storage_roots.scratch) + sum(
            storage.tree_gib(path) for path in storage.checkouts_of(owner)
        )
        if occupied + size > storage.scratch_cap_gib:
            return UnverifiedParity("Committed bus copy exceeds this caller's scratch allowance")
        if storage.free_gib() - size < storage.floor_gib:
            return UnverifiedParity("Insufficient free space for a committed bus copy")
        owner.storage_roots.scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="parity-rules-", dir=owner.storage_roots.scratch) as run:
            root = Path(run) / "bus"
            held = MaintenanceBarrier(config.root / "registry.json").witness_paths
            with _store_lock(comms.bus.path):
                CommittedStoreCopy.copy_tree(config.root, root, excluded=held)
            selected = config.model_copy(update={"root": root})
            CommanderConfig.root_path(root).write_text(selected.model_dump_json() + "\n")
            return cls.invoke(selected, (*cls.arguments, "--root", str(root)))

    def describe(self) -> str:
        observed = self.observation
        return (
            f"current_rules={len(observed.current.splitlines())}; "
            f"native_rules={len(observed.native.splitlines())}; matched={self.passed}; "
            f"owner={observed.source}; commander={observed.commander}; root={observed.root}; "
            f"private_state_unchanged={self.state_before == self.state_after}"
        )


class RuleReviewComparison(CommandComparison):
    arguments = RuleReviewObservation.arguments()


class OrderContactComparison(CommandComparison):
    arguments = OrderContactObservation.arguments()
