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

## Working checkpoint

The declared SelectedTriageOutcome family owns successful versus rejected model
results. Both preserve strict decoding. A rejected result atomically commits its
verified original native context and settles the original claim as FailedAssignment;
it publishes the existing non-waking failure alert and continues the inbox. It
never invents IGNORE/FULL, repeats the native input, or changes participant identity.

The original nullable SQL decision is classified at the existing FieldCodec
boundary into a TriageDecisionRecord. FailedTriageHistoricalNativeInput requires
the canonical failed claim and the corroborated native source, with no decision
field. Valid decisions retain their original proof semantics, including FULL
decisions followed by a later execution failure. Notification inflight projection
derives from absence of native context, rather than absence of a valid verdict.
No SQL format, schema, runtime store or native bundle is added.

Focused source sanity: 10 passed / 64 deselected in 6.33 seconds. These are
preparation only, not the installed-path evidence below.

Actual installed continuous ACP/native journey02: PASS, 23.98 seconds. Two real
native owners, real private bus/SQLite/ACP, unchanged trusted native0064, exactly
four localhost provider requests and zero paid/public requests:

1. Channel request and actual answerer native reply.
2. Questioner's native result is malformed bare IGNORE. Canonical failed claim,
   recorded native proof, non-waking failure alert and exact ACP cursor injection.
3. A distinct fresh channel source is automatically admitted and decided normally.
4. A fresh human ACP input receives an actual native terminal answer.

No original input is retried. All owner processes stop and the fixture server
thread exits. Receipt/originals remain in installed-continuity02/. The Core wheel
is actually installed, with 305 Python files byte-equal to source; dependencies
come from the accepted installed default484 donor, not a Core source overlay.
The first actual attempt01 is retained: its test incorrectly expected covered
prefix2 after the non-waking alert advanced coverage to3. Exact injected source
remained2. That assertion was corrected to preserve both authoritative meanings;
no runtime failure was hidden. Journey02 uses distinct fresh fixture inputs.

Patterns: IMPL-4 (finish the result family and all consumers), IDEN-3 (classify
external absence once), BOUND-1 (strict boundary decoding), TIME-9 (reuse sealed
FieldCodec). The malformed-result failure is distinct from unknown native send;
this checkpoint does not grant recovery or replay of uncertain original inputs.

Final integrated checkpoint bdac1c24 normally incorporates merged Core487.
Installed journey03 passed24.31s with the same continuous workflow and four
localhost requests; all306 installed Python files match this integrated source.
The final focused batch passed17 /64 deselected in10.38s, including canonical
Failed notification and sealed-codec ownership. Changed-source ratchet has zero
positive deltas. Production:27 lines deleted,155 added across8 modules.

State: scoped installed integration ready; not default live yet.
