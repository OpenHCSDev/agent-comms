# Selected triage contract and inbox continuity

Owner: parent integration agent. Base: Core #484 (`50d77a17`).

## Live reproducer

On the default installed runtime, `openhcs-helper2` repeatedly published an
`UnavailableDrainDiagnostic` with `IdentityConflict: triage response is not an
unambiguous decision`. The original activity events at Unix times
1790874363.0330877 and 1790874553.051047 fence the same incarnation and owner
generation 1234. No original input has been retried.

Its original selected native sessions at 17:08:56 and 17:09:17 UTC on October 1
contain a successful assistant stop with the text `IGNORE`. The declared prompt
requests `{"decision":"IGNORE"}`; `SelectedTriage.parse` requires that object.
The native result is neither a participant identity conflict nor permission to
retry an already admitted input. A later native session at 17:11:15 UTC contains
the declared JSON object. These are observations of distinct original inputs,
not retries performed by this investigation.

## Scope and acceptance

Trace the declared triage request, native result, original reservation, outcome
and drain as one workflow. Let the declaration own the response contract and
the settled failure disposition. Preserve strict decoding, source proof and
uncertain-input custody. Do not accept ambiguous prose, add a second parser or
retry the original input. Distinguish a malformed model result from an actual
identity/authority conflict, and ensure a failed selected decision cannot leave
all subsequent independent input unavailable.

Check the latest refactor-audit ownership patterns before implementation.
Verify malformed decision followed by a fresh valid decision and normal input
through the installed native/ACP path. Source tests prepare that journey; they
do not establish live readiness. Report deleted lines with the working patch.

State: investigation; no implementation or installed fix claimed yet.
