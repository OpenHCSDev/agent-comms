#!/usr/bin/env python3
"""Cap every comms agent's context at the declared token limit, on any model.

Tristan's rule: no agent's context goes above 300,000 tokens without
compacting, to save tokens. models-store.json keeps stating each model's real
window (it must not misstate the model); Pi's one compaction trigger —
shouldCompact, the shared decider behind the threshold check before a prompt,
the check before the next assistant response and overflow recovery — takes the
declared cap as its effective window instead. Manual /compact and overflow
recovery already compact unconditionally, so every path that reserves, commits
or recovers context honors the same single fact. Nothing else in Core decides
"compact now" (4c009c007 collapsed Core's engine into Pi's).
"""

import sys
from pathlib import Path

CONTEXT_TOKEN_CAP = 300_000

STOCK = """export function shouldCompact(contextTokens, contextWindow, settings) {
    if (!settings.enabled)
        return false;
    return contextTokens > contextWindow - settings.reserveTokens;
}"""
CAPPED = f"""// Core's declared context cap: no agent's context goes above this many tokens
// without compacting, on any model; the model's real window stays declared in
// models-store.json and is still used for everything else.
const contextTokenCap = {CONTEXT_TOKEN_CAP};
export function shouldCompact(contextTokens, contextWindow, settings) {{
    if (!settings.enabled)
        return false;
    return contextTokens > Math.min(contextWindow, contextTokenCap) - settings.reserveTokens;
}}"""


def main(compaction: Path) -> None:
    source = compaction.read_text()
    if source.count(STOCK) != 1:
        raise SystemExit("Native compaction trigger changed; the cap patch needs its new anchor")
    compaction.write_text(source.replace(STOCK, CAPPED, 1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
