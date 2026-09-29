"""Read-only coverage receipt projected from installed NRA declaration owners."""

import json
from dataclasses import asdict
from pathlib import Path

import nominal_refactor_advisor
from nominal_refactor_advisor.analysis import (
    DetectorTypePartition,
    default_detector_types_for_analysis,
)
from nominal_refactor_advisor.analysis_cache import DetectorRegistrySignature
from nominal_refactor_advisor.ast_tools import PythonSourcePathDiscovery, PythonSourcePathPolicy


def main():
    roots = (
        Path(__file__).resolve().parents[2] / "src/agent_comms",
        Path(
            "/home/ts/.local/share/agent-comms/runtime-watchdog-20260928/"
            "lib/python3.14/site-packages/toad"
        ),
        Path(
            "/home/ts/.local/share/agent-comms/runtime-watchdog-20260928/"
            "lib/python3.14/site-packages/textual"
        ),
    )
    members = default_detector_types_for_analysis()
    registered = DetectorRegistrySignature.current()
    requested = DetectorRegistrySignature.from_detector_types(members)
    partition = DetectorTypePartition(members)
    registered_keys = {member.registered_key for member in registered.detector_types}
    requested_keys = {member.registered_key for member in requested.detector_types}
    receipt = {
        "nra_import": nominal_refactor_advisor.__file__,
        "registered_inventory": asdict(registered),
        "requested_inventory": asdict(requested),
        "registered_count": len(registered.detector_types),
        "requested_count": len(requested.detector_types),
        "unrequested_registered_keys": sorted(registered_keys - requested_keys),
        "compact_context_count": len(partition.compact_global_detector_types),
        "ast_retaining_context_count": len(partition.ast_retaining_context_detector_types),
        "source_context": [
            {
                "root": str(root),
                "paths": [
                    str(path)
                    for path in PythonSourcePathDiscovery(
                        root, PythonSourcePathPolicy(include_tests=False)
                    ).paths()
                ],
            }
            for root in roots
        ],
        "scope": "requested declarations/source discovery, not independent execution attestation",
    }
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
