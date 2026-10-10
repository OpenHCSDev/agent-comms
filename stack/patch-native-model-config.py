#!/usr/bin/env python3
"""Read auth and models from the owner's canonical Pi configuration.

AGENT_COMMS_NATIVE_CONFIG_DIR names that directory when the writable agent
directory (PI_CODING_AGENT_DIR) is a separate acquired resource. It is only a
location; it switches no behavior. Settings, including compaction thresholds,
come from PI_CODING_AGENT_DIR and the project, as Pi's RPC mode reads them.
"""

import hashlib
import sys
from pathlib import Path

SOURCE_SHA = "3a4ee476b0596f346023398f52176381355f98dd621b8161f269c7ef3a57e28f"


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Native model services anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(services: Path) -> None:
    if hashlib.sha256(services.read_bytes()).hexdigest() != SOURCE_SHA:
        raise SystemExit("Native model services source does not match pinned build")
    source = services.read_text()
    anchor = "    const modelRuntime = options.modelRuntime ??\n"
    source = replace_once(
        source,
        anchor,
        "    const modelDir = process.env.AGENT_COMMS_NATIVE_CONFIG_DIR\n"
        "        ? resolvePath(process.env.AGENT_COMMS_NATIVE_CONFIG_DIR) : agentDir;\n" + anchor,
    )
    for name in ("auth.json", "models.json"):
        source = replace_once(source, f'join(agentDir, "{name}")', f'join(modelDir, "{name}")')
    services.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
