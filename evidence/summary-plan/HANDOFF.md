# Selected summary follows the native compaction plan

Real installed PR95 acceptance reached a selected summary but returned UNKNOWN
on history requiring more than four provider calls. Existing native regression
explicitly expected that larger history to fail. The wrapper's fixed total of
four calls conflicts with native compact()'s map/reduction plan; four is the
normal maximum parallel worker count, not a valid bound on total segments.

Remove that duplicate total-call constraint. The native algorithm still owns
segmentation, bounded concurrency and shrinking reductions. The selected slot
retains 90-second deadline, 1 MiB source, cumulative 256 KiB output, no retry,
exact route capture and join-before-release. UNKNOWN is never replay permission.
No provider or model changed. The wrapper calls only the captured selected stream.

All42 real native protocol/fake-provider cases pass. Larger case now summarizes
using7calls; existing3call case passes and oversizedsource is declined before
any call. Normal prepare-pi-native reproduces the updated complete-tree pin.
Actual configured-provider repeated retention run is pending; not claimed passed.
Parent will finish actual acceptance and deploy the paired Python/native package.

## Actual acceptance completed

Installed runtime-summary-candidate-20260928 with the physical pinned package
ran six actual ACP turns using configured openai-codex/gpt-6-sol. Three selected
summaries linked to three committed native compactions. Both retention answers
preserved ORCHID-7301, Thursday14:30, rejected cobalt and chosen cedar. Actual
context fell from11550 to2240 after the first full reduction. Run206.75seconds.
Only owned test project settings changed: explicitly trusted reserve263808 /
keep512 forces the272000-context model through the path without huge history.
No injected usage, fake provider, rewrites of history or replay of UNKNOWN.

Five bounded owner commit/disabled/settings/no-goal/correction cases pass.
Initial Python selection used a physical deployment package where the fixture
expects stack/bin/pi-native, giving missing-launcher failures. The corrected
combined selection exceeded60seconds; it is not claimed green. The bounded
five-case selection and42native cases are completed evidence.

PR95 still needs actual queued-input acceptance; whole-input-store ingress
revision checks may reject a follow-up admitted during a summary. Parent owns
that next real-path check/fix. This PR removes the verified four-call blocker.
