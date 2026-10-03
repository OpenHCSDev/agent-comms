"""Check the changed stock patch boundaries, without building a native bundle.

Only declaration/patch-selected source files are copied. This is neither a
runtime nor full-tree trust proof. No native child, provider or saved input runs.
"""

import ast
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

repo, scratch, stock = map(Path, sys.argv[1:])
target = scratch / "source-projection"
target.mkdir(parents=True, exist_ok=False)
stack = repo / "stack"
patches = [repo / "experiments/pi-context-proof/pi-0.85.1-native-input.patch"] + [
    stack / name for name in (
        "native-generation-policy.patch", "native-session-storage.patch",
        "native-request-progress-adapters.patch", "native-summary-prefix.patch",
    )
]
paths = {"package.json"}
for patch in patches:
    paths.update(line[6:] for line in patch.read_text().splitlines()
                 if line.startswith("--- a/"))
paths.update((
    "dist/core/agent-session-services.js", "dist/core/settings-manager.js",
    "dist/core/tools/write.js", "dist/core/tools/edit.js",
    "node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.js",
    "node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.d.ts",
    "node_modules/@earendil-works/pi-ai/dist/utils/retry.d.ts",
))
for relative in sorted(paths):
    source, destination = stock / relative, target / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
before = {name: hashlib.sha256((stock / name).read_bytes()).hexdigest()
          for name in sorted(paths)}


def patch(path):
    subprocess.run(["patch", "--batch", "--fuzz=0", "--no-backup-if-mismatch",
                    "-p1", "-d", str(target), "-i", str(path)], check=True)


def script(name, destination=target):
    subprocess.run([sys.executable, str(stack / name), str(destination)], check=True)


patch(patches[0])
script("patch-native-auto-compaction.py", target / "dist/core/agent-session.js")
script("patch-native-steering.py")
script("patch-native-model-config.py", target / "dist/core/agent-session-services.js")
script("patch-native-adaptive-settings.py")
script("patch-native-context-budget.py", target / "node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js")
patch(patches[1])
script("patch-native-selected-compaction-summary.py", target / "dist/modes/rpc/rpc-mode.js")
patch(patches[2])
script("patch-native-turn-context.py")
script("patch-native-file-artifact.py")
patch(patches[3])
patch(patches[4])
script("patch-native-request-observation.py")

compiled = [
    "dist/core/agent-session.js", "dist/core/compaction/compaction.js",
    "dist/modes/rpc/rpc-mode.js",
    "node_modules/@earendil-works/pi-agent-core/dist/agent-loop.js",
    "node_modules/@earendil-works/pi-agent-core/dist/agent.js",
    "node_modules/@earendil-works/pi-ai/dist/utils/provider-retry.js",
    "node_modules/@earendil-works/pi-ai/dist/utils/agent-comms-request-observation.js",
]
for name in compiled:
    subprocess.run(["node", "--check", str(target / name)], check=True)

summary = (target / compiled[1]).read_text()
loop = (target / compiled[3]).read_text()
session = (target / compiled[0]).read_text()
assert summary.count("new NativeRequestObservation(") == 1
assert summary.count("request.events(stream)") == 1
assert loop.count("request.events(response)") == 1
assert "let firstDelta" not in loop
assert session.count("this.agent.onRequestProgress =") == 1
assert "const source = await this._commitNativeContext(context);" in session
assert "onRequestProgress: this.agent.onRequestProgress" in session
assert before == {name: hashlib.sha256((stock / name).read_bytes()).hexdigest()
                  for name in sorted(paths)}
receipt = {
    "source_head": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
    "stock": str(stock), "copied_source_files": len(paths),
    "copied_source_bytes": sum((stock / name).stat().st_size for name in paths),
    "original_source_sha256": before,
    "generated_sha256": {name: hashlib.sha256((target / name).read_bytes()).hexdigest()
                         for name in compiled},
    "stock_unchanged": True, "zero_native_children_provider_inputs": True,
    "qualified": "Exact stock patch order and seven generated modules; not a native bundle, installed route or provider acceptance",
}
(scratch / "generated-source-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
print(json.dumps({"copied_source_files": len(paths), "copied_source_bytes": receipt["copied_source_bytes"],
                  "syntax_modules": len(compiled), "stock_unchanged": True}))
