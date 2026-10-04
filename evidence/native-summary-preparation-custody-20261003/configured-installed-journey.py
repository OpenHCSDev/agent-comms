"""One merged562 configured saved-fork journey, using the original shared driver."""
import asyncio
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys

here = Path(__file__).resolve().parent
checkout = here.parents[1]
sys.path.insert(0, str(checkout / "tests"))
sys.path.insert(0, str(checkout / "tools/cutover"))
from compaction_source_successor_installed_journey import run, digest
from original_owner_capture import CurrentTypedCapture
import agent_comms
from agent_comms.field_codec import FieldCodec
from agent_comms.pi_vocabulary import ThinkingLevel
from agent_comms.native_package import MANIFEST

stage = Path("/home/ts/.cache/agent-scratch/m562-configured")
package = Path("/home/ts/wt/comms-task-aware-native-bundle-20261002/stack/.pi-native-044646789787e3d3/node_modules/@earendil-works/pi-coding-agent")
root = Path("/var/tmp/agent-comms-live-20260927-wzjtqhza")
original_python = Path("/home/ts/.local/bin/agent-comms").resolve().with_name("python")


async def main():
    assert not stage.exists(), "Never replay an existing configured attempt"
    installed = Path(agent_comms.__file__).resolve().parent
    assert installed.is_relative_to(Path(sys.prefix))
    source = checkout / "src/agent_comms"
    hashes = {str(path.relative_to(source)): digest(path) for path in source.rglob("*")
              if path.is_file() and "__pycache__" not in path.parts}
    changed = [name for name, expected in hashes.items() if digest(installed / name) != expected]
    assert not changed, changed
    captured = CurrentTypedCapture(root, original_python).read("openhcs-audit-merged-boundaries")
    original = captured.require_current()
    original.require_idle()
    source_file = Path(original.require_saved_session())
    protected = [source_file, Path(str(source_file) + ".input-proof")]
    diagnostic = root / "diagnostics/79cb9232873a4501a118cc25da9fca67.json"
    if diagnostic.exists():
        protected.append(diagnostic)
    before = {str(path): digest(path) for path in protected}
    intent = {"head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip(),
              "interpreter": sys.executable, "installed_module": str(installed),
              "package": str(package), "native_manifest_sha256": digest(MANIFEST),
              "direct_url": json.loads(metadata.distribution("agent-comms").read_text("direct_url.json")),
              "sdk": metadata.version("agent-client-protocol"), "source_files_equal": len(hashes),
              "original_selection": FieldCodec.encode(captured.selection), "model": original.model,
              "thinking": ThinkingLevel.optional_name(original.thinking_level), "source_bytes": source_file.stat().st_size,
              "protected_before": before, "public_inputs": 0, "replay": False}
    (here / "configured-intent.json").write_text(json.dumps(intent, indent=2) + "\n")
    (here / "configured-source-proof.json").write_text(json.dumps(hashes, indent=2) + "\n")

    def original_capture():
        return captured.require_current(), captured.retained

    try:
        await run(stage, package, source_file, capture_source=original_capture,
                  probe_marker="SOURCE562_DISTINCT_AFTER_PREPARED_SUMMARY")
    finally:
        after = {str(path): digest(path) for path in protected}
        (here / "configured-protected.json").write_text(json.dumps({
            "before": before, "after": after, "equal": before == after}, indent=2) + "\n")
    assert before == after
    journey = json.loads((stage / "receipt.json").read_text())
    assert journey["complete"] and journey["native_children_closed"] and journey["distinct_input_started_once"]
    (here / "configured-receipt.json").write_text(json.dumps({"intent": intent, "journey": journey,
        "scope": "Actual configured provider, SDK/ACP selected summary followed by one distinct original input and terminal/checkpoint; no UI or controlled latency comparison.",
        "original_protected_unchanged": True}, indent=2) + "\n")


asyncio.run(main())
