# Retained native history regression fixed and installed

User reported the live native Toad view stuck behind its loading overlay after
deployment291. Earlier ACP attachment, wire history and isolated command checks
did not prove retained native history visibility. This was an acceptance gap.

Actual installed pr95-selected-pi-summary-owner native session reproduction
timed out with history widgets mounted. AgentReady had constructed an unrelated
presentation object instead of initializing Textual Message. Toad134 makes that
one-line correction; the same actual retained native view then became ready,
mounted TranscriptHistory and removed loading. Its Agent.run finished without
exception. No model prompt, owner stop, history rewrite or input replay.

Toad134 merged08ee5aa is installed in runtime-history-ready-20260928. Core4510dddf,
Textualc9743801 and native5fde unchanged. All five launchers point to that runtime;
existing core owners already run those same core bytes and were not restarted.
The user's existing Toad must restart to import the fix. The executed installer
was deleted. Exact provenance/activation and before/after receipts are alongside.

Future shipping acceptance must render a retained native conversation, assert
ready and loading-overlay removal, and exercise its history view. Initialize/load
or wire-DM rows cannot substitute for this check. General initialization failure
feedback is owned by Carver in Toad133; remaining refactor/integration goal stays
active. CI deferred.
