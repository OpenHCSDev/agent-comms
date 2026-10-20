"""agent-comms install: the bus it creates or practises on, and the route to it."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from agent_comms.active_route import ActiveRoute, read_active_route, route_new_bus
from agent_comms.candidate_maintenance import drain_candidate_catchup
from agent_comms.comms import Comms
from agent_comms.coordination_database import CoordinationStore
from agent_comms.installer.backup import bus_files
from agent_comms.installer.carry import CarryChain, CarryLedger
from agent_comms.installer.practice import practice_copy
from agent_comms.maintenance_barrier import MaintenanceBarrier, MaintenanceOperator
from agent_comms.wire_log import WireLog

REPO = Path(__file__).resolve().parents[1]


def bus(directory: Path) -> tuple[Path, str]:
    """A private bus holding one thread and one message, as production writes one."""
    root = directory / "wire"
    root.mkdir(mode=0o700)
    comms = Comms(root)
    comms.registry.declare(comms.registry.new_thread("worker", frozenset({"team"}), str(directory)))
    root_id = comms.messaging.initialize_private_initial_protocol()
    comms.messaging.send("worker", "#team", "hello")
    drain_candidate_catchup(root)
    return root, root_id


def test_a_bus_install_creates_is_fresh_until_marked_and_runs_no_carry(private_directory):
    root = private_directory / "bus"
    root.mkdir(mode=0o700)
    log = WireLog(WireLog.path_in(root))
    assert log.fresh()
    Comms(root).messaging.initialize_private_initial_protocol()
    assert not log.fresh()
    chain = CarryChain.at(REPO)
    CarryLedger(root).start(chain)
    assert chain.pending(CarryLedger(root).applied()) == ()


def test_install_routes_a_new_bus_and_replaces_only_a_route_that_selects_none(private_directory):
    first, first_id = bus(private_directory)
    (private_directory / "second").mkdir()
    second, second_id = bus(private_directory / "second")
    directory = private_directory / "route-state"
    directory.mkdir(mode=0o700)
    route_file = directory / "active-route.json"

    route_new_bus(ActiveRoute(first, first_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(first, first_id)
    route_new_bus(ActiveRoute(second, second_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(first, first_id)

    first.rename(first.with_name("retired"))
    # A route whose bus is gone still decodes, so an explicit --root is never refused by it.
    assert read_active_route(route_file) == ActiveRoute(first, first_id)
    route_new_bus(ActiveRoute(second, second_id), route_file)
    assert read_active_route(route_file) == ActiveRoute(second, second_id)
    (backup,) = directory.glob("active-route.*.bak.json")
    assert json.loads(backup.read_text())["root"] == str(first)


def test_practice_copy_is_the_bus_as_files_without_its_maintenance_witnesses(private_directory):
    root, _ = bus(private_directory)
    (root / "storage.json").write_text(json.dumps({"floor_gib": 0}))
    barrier = MaintenanceBarrier(root / "registry.json")
    barrier.begin(MaintenanceOperator.current())
    witnesses = {str(path.relative_to(root)) for path in barrier.witness_paths if path.exists()}
    assert witnesses
    saved = bus_files(root)
    databases = {name for name in saved if CoordinationStore.is_database(root / name)}

    with practice_copy(root, private_directory / "practice/bus") as (copy, measurement):
        copied = bus_files(copy)
        assert copied.keys() == {name for name in saved.keys() - witnesses
                                 if not (name.endswith("-wal")
                                         and name.removesuffix("-wal") in databases)}
        assert measurement.bytes > 0
        for name in databases:
            query = "SELECT name FROM sqlite_schema ORDER BY name"
            with (closing(sqlite3.connect(root / name)) as original,
                  closing(sqlite3.connect(copy / name)) as practised):
                assert practised.execute(query).fetchall() == original.execute(query).fetchall()
        for name in copied.keys() - databases:
            assert (copy / name).read_bytes() == (root / name).read_bytes()
    assert bus_files(root) == saved


def test_install_report_encodes_its_registered_harnesses() -> None:
    # The live install printed nothing: its report's field type had no codec.
    from agent_comms.cli_commands import InstallResult
    from agent_comms.field_codec import FieldCodec

    report = InstallResult("/config.json", {"claude-code": ("skill", "tools")})
    assert FieldCodec.decode(InstallResult, FieldCodec.encode(report)) == report
