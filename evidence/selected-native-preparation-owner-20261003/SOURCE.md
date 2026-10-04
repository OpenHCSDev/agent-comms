# Selected native preparation owner

Base: 0608319c02c5109c7b852e80c34841dec12bda5b (568 merged).

The selected idle native child already owns its loaded DiskEntryStore. The owner
runtime currently launches a detached preparation helper twice, each building a
second full-history index: once for the initial cut, once with captured retained
facts. The two budgets are real distinct inputs; the repeated store construction
is unnecessary. Move selected preparation to a declaration-owned read-only query
of that original child, through the existing selected observation/custody lifetime.
Keep the original witness, settings/model, captured facts, writer CAS and uncertain
transport retirement. No paid calls or source writes during preparation.

The standalone preparation helper has a different resource contract: readers with
no running selected child. It uses the same pinned prepareCompaction algorithm;
it must not become a fallback if selected-child custody or observation fails.

Patterns: IMPL-13 (resource implementation repeated across selected owner and
standalone helper), BOUND-2 (loaded native store bypassed at the selected boundary).

Original configured request: dispatch .873s, first event 1.099s, finish 98.141s;
39,629 input and 3,176 output tokens. Those clocks do not separate provider
processing, reasoning or generation. The separate finish-to-next-request gap is
13.562s. Neither span is claimed fixed by removing pre-summary index construction.

AST before: src/agent_comms, tests, tools, 726 modules, no parse omissions.
Lexical references are evidence, not dynamic-resolution proof. Read all matched
owner and boundary consumers before migration. Native source/dependency AST
coverage follows the existing bundled Acorn census.
