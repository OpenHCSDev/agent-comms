#!/usr/bin/env python3
"""Keep native retry isolation while using the owner's canonical model runtime."""

import hashlib
import sys
from pathlib import Path

SOURCE_SHA = "3a4ee476b0596f346023398f52176381355f98dd621b8161f269c7ef3a57e28f"


def main(services: Path) -> None:
    if hashlib.sha256(services.read_bytes()).hexdigest() != SOURCE_SHA:
        raise SystemExit("Native model services source does not match pinned build")
    source = services.read_text()
    anchor = "    const modelRuntime = options.modelRuntime ??\n"
    if source.count(anchor) != 1:
        raise SystemExit("Native model services constructor changed")
    source = source.replace(
        anchor,
        "    const modelDir = process.env.AGENT_COMMS_NATIVE_CONFIG_DIR\n"
        "        ? resolvePath(process.env.AGENT_COMMS_NATIVE_CONFIG_DIR) : agentDir;\n" + anchor,
        1,
    )
    for name in ("auth.json", "models.json"):
        anchor = f'join(agentDir, "{name}")'
        if source.count(anchor) != 1:
            raise SystemExit("Native model services path changed")
        source = source.replace(anchor, f'join(modelDir, "{name}")', 1)
    services.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
