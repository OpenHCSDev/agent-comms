# Nominal tool request closure

Original S7/A2 closure:29 nominal ToolRequest cases derive names/catalog/parameter
JSON Schema/context binding from their actual declarations. Deleted central TOOLS
and _TOOLS_BY_NAME, ToolDeclaration/ToolParameter, handler-function roster, raw
request decoder and post-decode string/bool/enum recasts. Existing Command,
DeclaredFamily and canonical FieldCodec reused; FieldCodec gains declaration-derived
record schema and constrained-owner choices, no codec subclass. Four exact matching
operations reuse existing CliCommand behavior; GoalAction remains goal authority.
External29 MCP/Pi catalog schemas remain exactly equal to the pre-change capture.

Actual current callers migrate to request declaration lookup. No native_pi, Toad
App or workspace edits; current installation/history/format unchanged. Focused
behavior and installed/native tool/context acceptance in progress on this same PR.
CI deferred. Author-owned AST moves are labeled as authored; no native NRA proof
or zero-debt claim. Parent full-package NRA raw receipt is a structural lead;
manual semantic ownership of this adapter is the refactor authority.
