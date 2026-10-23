"""Old failure labels in the goal store are carried under the install's maintenance hold."""

import sqlite3
from contextlib import closing

from agent_comms.goal_attempts import GoalAttemptStore
from agent_comms.goal_failure_observation import FailedTurnEvidence
from agent_comms.locked_store import RootFile
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.registry_store import RegistryStore
from cutover.carry_declared_failures import DeclaredFailures
from goal_owner_fixture import canonical_goal_wire


def test_every_declared_location_inverts_to_its_bus_root(tmp_path):
    for family, directory in RootFile.declarations_in(tmp_path):
        if "{" not in family.filename:
            assert family.bus_root(family.path_in(directory)) == tmp_path


def test_old_labels_carry_in_the_goal_store_the_bus_declares(tmp_path):
    comms = canonical_goal_wire(tmp_path)
    store = comms.goals.open_goal_store().path
    assert store == GoalAttemptStore.path_in(GoalAttemptStore.directory_in(comms.root))
    with closing(sqlite3.connect(store)) as db:
        db.execute(f'ALTER TABLE "{FailedTurnEvidence.declared_name}" RENAME COLUMN failure TO reason')
        db.commit()
    step = DeclaredFailures(comms.root, comms.root, tmp_path / "spill")
    assert store in step.backup_files()
    assert step.carry(apply=False).changed == 1
    hold = MaintenanceBarrier(RegistryStore.at(comms.root).path).begin(MaintenanceOperator.current())
    with hold.holding():
        assert step.carry(apply=True).changed == 1
    with closing(sqlite3.connect(store)) as db:
        assert "failure" in FailedTurnEvidence.stored_columns(db)
    assert step.carry(apply=False).changed == 0
