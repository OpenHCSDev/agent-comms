"""Verify the frozen installed cohort before any native/application input."""

import hashlib
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import subprocess
import sys


stage = Path("/home/ts/.local/share/agent-comms/runtime-native-source-queue-cohort-20260930")
evidence = Path("/home/ts/.cache/agent-scratch/native-source-queue-cohort-stage-20260930")
assert Path(sys.prefix) == stage
components = {
    "agent-comms": ("agent_comms", "970bc527f4ddd9b3bde5522dc671aea11fd27ece",
                    Path("/home/ts/wt/comms-bus-projection-ownership-s14-20260930"), "src/agent_comms"),
    "batrachian-toad": ("toad", "d3ba4cf330acd4d2eec6cd806fab8113c3a046ec",
                       Path("/home/ts/wt/toad-native-custody-pair-20260930"), "src/toad"),
    "textual": ("textual", "6b5895fa0a72aeec2aeaef7206d5debfa0c1803c",
                Path("/home/ts/wt/textual-lazy-geometry-publication-damage-20260930"), "src/textual"),
}
source_proof = {}
provenance = {}
for distribution, (module, revision, repo, prefix) in components.items():
    installed = metadata.distribution(distribution)
    direct = json.loads(installed.read_text("direct_url.json"))
    assert direct["vcs_info"]["commit_id"] == revision
    assert not direct.get("dir_info", {}).get("editable", False)
    origin = Path(importlib.import_module(module).__file__).parent
    assert origin.is_relative_to(stage)
    files = subprocess.check_output([
        "git", "-C", str(repo), "ls-tree", "-r", "--name-only", revision, "--", prefix
    ], text=True).splitlines()
    hashes = {}
    for path in files:
        original = subprocess.check_output(["git", "-C", str(repo), "show", f"{revision}:{path}"])
        target = origin / Path(path).relative_to(prefix)
        assert target.is_file(), f"Missing original package file: {path}"
        assert target.read_bytes() == original, f"Original package bytes differ: {path}"
        hashes[path] = hashlib.sha256(original).hexdigest()
    source_proof[distribution] = {
        "revision": revision, "origin": str(origin), "original_files_equal": len(hashes),
        "python_files_equal": sum(path.endswith(".py") for path in hashes), "sha256": hashes,
    }
    provenance[distribution] = direct
audit = metadata.distribution("nominal-refactor-audit")
audit_url = json.loads(audit.read_text("direct_url.json"))
assert audit.version == "0.1.0"
assert audit_url["vcs_info"]["commit_id"] == "d392c5e4cd189ce1203127a746337ad498a0330d"
assert metadata.version("agent-client-protocol") == "0.12.1"
from agent_comms.native_pi import _trusted_package
from agent_comms.native_package import MANIFEST
native = Path("/home/ts/.local/share/agent-comms/native-current-593b978a717ae8f6/"
              "node_modules/@earendil-works/pi-coding-agent")
_trusted_package(native)
manifest = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
assert manifest == "593b978a717ae8f6ab4300a1a107b615c8ced52e767ef4cad0596f827041a218"
force_files = {
    "stack/pi-native.sha256": "_native/pi-native.sha256",
    "stack/native-compaction-commit-child.mjs": "_native/native-compaction-commit-child.mjs",
}
core_origin = Path(source_proof["agent-comms"]["origin"])
for source, target in force_files.items():
    expected = subprocess.check_output([
        "git", "-C", str(components["agent-comms"][2]), "show",
        f"{components['agent-comms'][1]}:{source}"
    ])
    assert (core_origin / target).read_bytes() == expected, source
modules = ["agent_comms.private_nk_entrypoint", "agent_comms.active_route",
           "agent_comms.cohort_foreground", "agent_comms.session_lifecycle",
           "agent_comms.debt_ratchet", "agent_comms.compaction_boundary",
           "toad.widgets.conversation", "toad.widgets.viewport_body",
           "toad.widgets.presentation_window", "toad.screens.comms"]
for module in modules:
    importlib.import_module(module)
receipt = {
    "state": "PACKAGE-SOURCE-TRUST-PASS", "stage": str(stage), "sdk": "0.12.1",
    "distribution_count": len(list(metadata.distributions())), "source_proof": source_proof,
    "provenance": provenance, "audit_distribution": audit_url, "affected_imports": modules,
    "native_package": str(native), "native_manifest_sha256": manifest,
    "native_full_trust": True, "force_included_original_files_equal": list(force_files),
    "public_effects": 0, "native_inputs": 0, "provider_calls": 0,
    "scope": "Package/source/native trust only; private runtime preflight and both actual gates remain",
}
(evidence / "package-source-trust.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps({"state": receipt["state"], "distribution_count": receipt["distribution_count"],
                  "source_counts": {name: data["original_files_equal"]
                                    for name, data in source_proof.items()},
                  "native_full_trust": True}), flush=True)
