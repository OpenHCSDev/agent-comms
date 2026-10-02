# One coverage read carries its original native evidence

Base: Core b9fffaab169c6db21df7db142d18f7c7104c379e.

The existing SourceCoverage prefix operation corroborates each selected HistoricalNativeInput, then NativeSourceCursor asks SourceCoverage.last_proof and SourceCoverage.evidence to query and corroborate those same inputs again. Keep the actual observed inputs on the existing ProvenSourceCoverage result and derive sequence/reporting/last-proof behavior from them. Migrate both cursor advance and cursor read and delete the repeated readers.

This is one bounded operation's detached read result, not a persistent proof cache or replay capability. Retain original source-byte validation, UNKNOWN barriers, current SQL row comparison, canonical wire witness and owner/participant custody. Read replacement/append and schema boundaries before changing consumers. No native, SDK, journal format, runtime operator or public configuration changes. No claim that this unmeasured publication work explains the separate 8–16 second terminal-retirement gap.

Integration owner: Mendel. Einstein owns terminal proof and retirement measurements in #544. Arendt has released overlapping source; Sch owns the frozen #334 receiving/operator. Reuse the existing isolated checkout. Source and AST first; one affected source/installed read acceptance batch last, no new provider input or benchmark.
