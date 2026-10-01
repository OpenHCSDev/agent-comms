"""Existing recorder admission against a fresh canonical no-input private root."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--stage", type=Path, required=True)
parser.add_argument("--evidence", type=Path, required=True)
parser.add_argument("--private-root", type=Path, required=True)
parser.add_argument("--metadata-repo", type=Path, required=True)
args = parser.parse_args()
stage, base, root = args.stage, args.evidence, args.private_root
assert Path(sys.prefix) == stage
assert root.is_relative_to("/home/ts/wt")
assert not root.exists(), "Never reset or reuse a previous private root"
from agent_comms.comms import Comms
comms = Comms(root)
root_id = comms.messaging.initialize_private_initial_protocol()
source = json.loads((base / "package-source-trust.json").read_text())
pins = {name: proof["revision"] for name, proof in source["source_proof"].items()}
receipt = {**source, "pins": pins, "owner": "Schrodinger460",
           "frozen_package_count": source["distribution_count"],
           "private_root_witness": {"root": str(root), "wire_root_id": root_id,
                                    "producer": "actual installed Core Comms/initialize_private_initial_protocol",
                                    "owner_starts": 0, "native_inputs": 0},
           "accepted_scope": "Packaging/source/native trust and original RuntimeSelection preflight only",
           "remaining_scope": "Einstein joint native/ACP/PTY gate and460 hotDM/IRC/tab-return gate required"}
staging = stage / "staging-receipt.json"
staging.write_text(json.dumps(receipt, indent=2) + "\n")
(base / "paired-staging-receipt.json").write_bytes(staging.read_bytes())
path = args.metadata_repo / "tests/tools/record_installed_tui.py"
spec = importlib.util.spec_from_file_location("cohort_runtime_recorder", path)
recorder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recorder
spec.loader.exec_module(recorder)
env = {key: value for key, value in os.environ.items()
       if key != "PYTHONPATH" and not key.startswith(("AGENT_COMMS_", "TOAD_VIDEO_CAPTURE_"))}
env.update(AGENT_COMMS_RUNTIME_ROOT=str(stage / "bin"),
           AGENT_COMMS_ACP_LAUNCHER=str(stage / "bin/agent-comms-acp"),
           AGENT_COMMS_ROOT=str(root), AGENT_COMMS_PRIVATE_NK_WIRE_ROOT_ID=root_id,
           AGENT_COMMS_PRIVATE_NK_NATIVE_PACKAGE=source["native_package"],
           TOAD_VIDEO_CAPTURE_TARGET=recorder.PrivateCapture.declared_name)
from agent_comms.private_nk_entrypoint import PrivateNkLaunch
PrivateNkLaunch.from_environment(root, env).validate()
command = [str(stage / "bin/toad")]
selection = recorder.RuntimeSelection.from_environment(command, env)
owner = recorder.ProcessOwner()
try:
    preflight = selection.publish_verified_stage(staging, owner, env, command)
    (base / "paired-runtime-preflight.json").write_text(json.dumps(preflight, indent=2) + "\n")
finally:
    cleanup = owner.cleanup()
    (base / "paired-probe-cleanup.json").write_text(json.dumps(cleanup, indent=2) + "\n")
assert not cleanup["remaining_owned_pids"] and not cleanup["errors"]
for module, name in [("agent_comms", "agent-comms"), ("toad", "batrachian-toad"), ("textual", "textual")]:
    assert preflight["observed"]["packages"][module]["direct_url"]["vcs_info"]["commit_id"] == pins[name]
ready = {"state": "PACKAGE-READY-AWAITING-ACTUAL-JOINT-AND-BUS-GATES",
         "stage": str(stage), "pins": pins, "sdk": "0.12.1",
         "native_package": source["native_package"], "native_manifest_sha256": source["native_manifest_sha256"],
         "distributions": source["distribution_count"], "native_full_trust": True,
         "all_original_package_files_equal": {name: proof["original_files_equal"]
                                              for name, proof in source["source_proof"].items()},
         "runtime_preflight_pass": True, "probe_cleanup": cleanup,
         "private_root_witness": receipt["private_root_witness"], "public_effects": 0,
         "native_inputs": 0, "provider_calls": 0, "build_count": 1,
         "staging_receipt_sha256": hashlib.sha256(staging.read_bytes()).hexdigest(),
         "activation_sha256": hashlib.sha256((stage / "activation.json").read_bytes()).hexdigest(),
         "remaining_scope": receipt["remaining_scope"]}
(base / "paired-ready-receipt.json").write_text(json.dumps(ready, indent=2) + "\n")
print(json.dumps(ready), flush=True)
