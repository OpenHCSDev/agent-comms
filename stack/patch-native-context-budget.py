#!/usr/bin/env python3
"""Integrate token-budget negotiation before the existing provider stream starts."""
import hashlib
from pathlib import Path
import sys

SOURCE_SHA = "1e2097ced37cf0e21aa5711297eecc77916de8a4ed81a9019bc7d97b22825fa3"


def main(provider: Path) -> None:
    if hashlib.sha256(provider.read_bytes()).hexdigest() != SOURCE_SHA:
        raise SystemExit("Native completion provider differs from the pinned source")
    source = provider.read_text()
    anchor = 'import { retryProviderRequest } from "../utils/provider-retry.js";'
    request = "const { data: openaiStream, response } = await retryProviderRequest(() => client.chat.completions.create(params, requestOptions).withResponse(), {"
    if source.count(anchor) != 1 or source.count(request) != 1:
        raise SystemExit("Native provider request boundary changed")
    source = source.replace(anchor, anchor + '\nimport { ContextBudgetRequest, BudgetAdmissionError } from "./agent-comms-context-budget.js";\nimport { chatInput } from "./agent-comms-request-input.js";', 1)
    source = source.replace(request,
        'if (params.max_tokens !== undefined && params.max_completion_tokens !== undefined) {\n'
        '                throw new BudgetAdmissionError("Request declares two competing output allowances");\n'
        '            }\n'
        '            const budgetField = params.max_tokens !== undefined ? "max_tokens" : compat.maxTokensField;\n'
        "            const budgetRequest = new ContextBudgetRequest(model, context, params, budgetField, "
        "payload => client.chat.completions.create(payload, requestOptions).withResponse(), "
        "chatInput(params), options);\n"
        "            const { data: openaiStream, response } = await retryProviderRequest(() => budgetRequest.send(), {", 1)
    provider.write_text(source)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
