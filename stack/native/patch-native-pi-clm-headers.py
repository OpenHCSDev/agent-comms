#!/usr/bin/env python3
"""pi-clm's malformed-header rejection names each line and the header rule.

pi-clm 1.0.0 rejected an edit with only "Mirror contains malformed headers for the current
document.", so the model could not see what to fix and retried blind: on 2026-11-20 CLM
invented `id=2-HOTFIX_ACCEPTED`, whose `_` the CTX_TURN id rule [a-zA-Z0-9-]+ refuses, and
the rejection never said so. The rejection now quotes up to three offending lines and states
the rule, so the next attempt can succeed. pi-clm is third-party (@lolipopshock/pi-clm, one
release), so the change is carried here against its pinned source.
"""

import hashlib
import sys
from pathlib import Path

DOCUMENT_SHA = "dd3d4fc60ae3e8ed7eea74f324332a4ab2f97dcb7c22bde9f997729786011f17"
ORIGINAL = """		return rejected(snapshot, beforeEstimate, "Mirror contains malformed headers for the current document.");
"""
REPLACEMENT = """		const shown = parsed.malformedCurrentHeaders.slice(0, 3).map((line) => JSON.stringify(line)).join("; ");
		return rejected(
			snapshot,
			beforeEstimate,
			`Mirror contains malformed headers for the current document: ${shown}. A header must read ` +
				`[[CTX_TURN document=${snapshot.documentId} index=<n> role=<role> id=<id> protected=<true|false>]] ` +
				"with an id of letters, digits and hyphens only; a new block's id is new-<name>.",
		);
"""


def main(source: str) -> None:
    path = Path(source) / "src/context-document.ts"
    text = path.read_text()
    digest = hashlib.sha256(text.encode()).hexdigest()
    if digest != DOCUMENT_SHA:
        raise SystemExit(f"pi-clm context-document.ts changed ({digest}); re-derive this patch")
    if text.count(ORIGINAL) != 1:
        raise SystemExit("pi-clm malformed-header rejection is not where this patch expects it")
    path.write_text(text.replace(ORIGINAL, REPLACEMENT))


if __name__ == "__main__":
    main(sys.argv[1])
