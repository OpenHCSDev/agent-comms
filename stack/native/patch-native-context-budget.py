#!/usr/bin/env python3
"""Reserve output budget from the estimator's measured error, not a fixed count.

The chars/4 estimator undercounted a 148,255-token DeepSeek V4.1 Flash request
by 7,140 tokens (4.8%) on 2026-11-04. A fixed 4,096-token reserve is 0.4% of a
1M window, so it cannot absorb an error that scales with the input. Reserve 10%
of the estimate (twice the measured error) with the existing 4,096 floor.
"""

import hashlib
import sys
from pathlib import Path

SOURCE_SHA = "4e0184d73736b60c2e32acc12a4b4eebad1324d7f61a4c955e896b3a1239db8d"

OLD = """const CONTEXT_SAFETY_TOKENS = 4096;
const MIN_MAX_TOKENS = 1;
export function clampMaxTokensToContext(model, context, maxTokens) {
    if (model.contextWindow <= 0)
        return Math.max(MIN_MAX_TOKENS, maxTokens);
    const available = model.contextWindow - estimateContextTokens(context).tokens - CONTEXT_SAFETY_TOKENS;
    return Math.min(maxTokens, Math.max(MIN_MAX_TOKENS, available));
}
"""

NEW = """const CONTEXT_SAFETY_TOKENS = 4096;
const CONTEXT_SAFETY_RATIO = 0.1;
const MIN_MAX_TOKENS = 1;
export function clampMaxTokensToContext(model, context, maxTokens) {
    if (model.contextWindow <= 0)
        return Math.max(MIN_MAX_TOKENS, maxTokens);
    const estimatedInputTokens = estimateContextTokens(context).tokens;
    const safetyTokens = Math.max(CONTEXT_SAFETY_TOKENS, Math.ceil(estimatedInputTokens * CONTEXT_SAFETY_RATIO));
    const available = model.contextWindow - estimatedInputTokens - safetyTokens;
    return Math.min(maxTokens, Math.max(MIN_MAX_TOKENS, available));
}
"""


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"Native context budget anchor changed: {old[:80]!r}")
    return source.replace(old, new, 1)


def main(options: Path) -> None:
    digest = hashlib.sha256(options.read_bytes()).hexdigest()
    if digest != SOURCE_SHA:
        raise SystemExit(f"Native context budget source does not match pinned build: {digest}")
    options.write_text(replace_once(options.read_text(), OLD, NEW))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
