# Selected-summary failure diagnostics

The native adapter discarded provider failure details after joining its streams;
Python then wrapped every failure as generic uncertainty. Preserve a bounded,
single-line native reason on UNKNOWN, validate it at the existing RPC boundary,
and retain the real provider message in SelectedChildUnknown. Transport timeouts
name their actual duration. Missing/malformed detail remains uncertain. No reason
is an admission token, settled journal record or permission to retry.

Native stream and lifecycle behavior is unchanged: one captured configured route,
no retries, joined retirement, output/source/deadline bounds, original source and
UNKNOWN journal fences. No new error/status dispatch family or parallel store.

Local acceptance:
-21 Python transport/journal cases passed;2 native cases skipped in that command.
-44 real native RPC/SDK cases using synthetic provider streams passed, including
  provider402 reason, bounded/control-normalized diagnostics, deadline/cancellation,
  no replay,3-call and7-call native plans, and prestart oversized-source decline.
-2 Python-to-native RPC cases separately passed (success and402 error). The error
  reaches Python unchanged while the journal remains UNKNOWN and input prohibited.
-5 owner integration cases passed,16 intentionally deselected.
-Ruff/Black/source syntax/diff checks pass. Normal prepare-pi-native reproduced the
  changed package; existing live package was never mutated. CI deferred.

Prepared candidate: runtime-diagnostics-20260928 and native-diagnostics-20260928
under ~/.local/share/agent-comms. Not yet activated. Darwin owns the adjacent ACP
TurnRunner exception-to-existing-events.Error delivery and caller/deletion closure.
Parent owns combined installation and actual live acceptance. No real provider
failure was induced; the error checks use the actual RPC/native path with local
synthetic provider streams.
