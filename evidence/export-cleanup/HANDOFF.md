# PR125 export compatibility deletion closure

Replaced the enum-emulating _FormatMeta, parse/value adapters and six scope/limit forwarding factories with direct declaration consumers. CLI boundary still uses the existing declaration FieldCodec; exporter trusts its typed format. Migrated every source/test caller in core and paired Toad current UI caller (refactor/export-caller-migration-20260928). External export records remain the single canonical representation, with existing byte/receipt fixtures unchanged.

Verification:
- 103 tests passed: export behavior/families, coordination cohort, envelope integration, CLI.
- Mounted current Toad main_menu_transfer_pilot passed against both updated source trees, executing actual core export and stopped-thread import.
- Ruff and git diff --check passed.
- NRA full package context scan was attempted with one parse/analysis worker and a 165-second bound; it timed out before emitting scan_status/coverage. This is an authored caller/deletion migration validated by execution, not a native equivalence or completed global NRA proof.
- Initial test interpreter lacked metaclass_registry; no tests ran under that interpreter. Successful run used the existing integration Python 3.14 test environment.

Commands:
PYTHONPATH=src timeout 60 /home/ts/wt/comms-refactor-integration-20260927/.venv/bin/python -m pytest -o addopts='' tests/test_exporting.py tests/test_export_families.py tests/test_coordination_cohort.py tests/test_envelope_bus_integration.py tests/test_cli.py -q
PYTHONPATH=<core>/src:<toad>/src:<toad>/tests timeout 60 /home/ts/.local/share/agent-comms/runtime-diagnostics-20260928/bin/python tests/main_menu_transfer_pilot.py

Parent owns combined core/Toad pinning, installation and activation. Already merged refactors with remaining shims are tracked in the dispatch DELETION-AUDIT.md and remain required work.
