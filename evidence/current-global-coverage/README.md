# Original / round2 global coverage completion audit

Source snapshot: core main `c338e8ab` in the persistent T2 audit branch;
installed Toad140 `436514d8`, Textual `c9743801`. NRA installed main
`ab85aa0b0724894f81e16e9fda30fc290fc0aa27` (merged R1 PR9), imported from
`~/wt/nra-installed-main-20260928`, `/usr/bin/python`3.14.6.
No production edits, migration, provider calls, live changes or duplicated test
matrix. Parent's307/308 and141/129 integration remains separate.

## Authoritative inventory recovered and executed

`inventory.py` projects `default_detector_types_for_analysis`,
`DetectorRegistrySignature`, `DetectorTypePartition`, and
`PythonSourcePathDiscovery`. The JSON is a receipt, not a new registry.
All81 registered detectors are requested; no registered key is omitted.
No context detector requires retained AST instead of compact global projection.
`detector-capabilities.json` is the existing declaration-derived CLI inventory,
including required relations, source sites and MRO/capability contributions.

| Actual execution | Scope/result |
| --- | --- |
| Full/raw CLI, explicit core context, no cache,150s internal budget | 227 production files;22.862s;114 raw findings:56 redundant type checks,33 unmodeled shapes,24 semantic mirrors,1 repeated builder. Full graph includes78 mapping-read projections. |
| Global CLI loop, explicit core+Toad+Textual context, no cache | 694 files;102.839s;`exact_compact_global`, **81 analyzed /0 omitted /complete true**;139 raw findings reported by counts, not an empty scan. |
| Existing compact API, same694-file scope, no cache; full raw projection |101.999s (95.850 preparation,6.148 analysis);139 actual raw findings; result's own cache identity supplies694 source identities and the81-detector registry, exactly matching the derived requested inventory. Counts match independent CLI. |

The API receipt uses the existing `JsonScanStatus.exact_compact_global` and
`json_report_object` owners. No invented coverage enum, registry or detector
allowlist. `paired-raw.json` is the actual result; `summary.json` exposes every
unmodeled key set and type-check source/declaration pair plus mapping-read leads.

This closes the missing **requested detector/source inventory** evidence. It
does not certify every imported third-party/stdlib/native-JS source, every
possible Python behavior, or zero architectural debt. ACP SDK/Pydantic schema
recognition and dynamic codecs are not claimed universally modeled. Source
context is explicit, rather than inferred from a focused local scan.

## R1 receipt and ownership interpretation

Paired raw counts: semantic mirrors49, unmodeled shapes33, redundant checks56,
repeated builder1. These are source leads, not 139 automatically valid deletion
instructions. Wider context adds25 semantic relationships; it does not silently
change the33/56 raw-record observations.

- `mapping_read` is genuinely collected (78 projections in the complete core
  graph), and7 paired raw mirror leads identify such projections. R1's decoder
  descent/positive/negative fixtures and <=25% calibration were already executed
  in NRA PR9; its owner is refactor-r1/Nietzsche. No calibration matrix repeated.
-47/56 type-check leads occur in `__post_init__`. For example LiveAttempt's
  exact integer lease check is the public constructor's validation boundary:
  annotations alone do not reject bool/invalid direct construction. Do not
  delete it on the detector's suggestion alone. The remaining9 include native
  proof verification, captured Message/admission and append-hint checks; source
  and declaration sites are paired in the receipt for actual caller tracing.
- Unmodeled shapes include `os.environ`, Field metadata, import-file/native/tool
  external records, and actual internal protocol dictionaries. Those have
  different authority contracts. No new record type is authorized merely by
  their shared key count.
- **Concrete internal followthrough candidate:** `CommsAgent._compact_request`
  still manually consumes `ok/error/summary/commitId` from worker/manual-owner
  results; `owner_compaction_manual.py` returns that dictionary and the bridge
  also reads result fields. This is live internal result dispatch, independently
  verified in source, not an environmental record. Route a scoped nominal
  compaction-result/caller closure to the existing S9/T2 owners; first reuse the
  current result/update/runtime owners. Preserve external reply shape. This audit
  does not duplicate parent312 or edit compaction/native files.

## Exact tool gap and coordination

Full/raw CLI currently omits `scan_status`: its noncompact analysis branch never
sets that variable. The capability inventory exists, and compact CLI/API supply
complete requested-roster evidence; therefore this is a reporting seam, not a
reason to hold merges. Reported to the existing NRA owner on
[PR9](https://github.com/OpenHCSDev/NominalRefactorAdvisor/pull/9#issuecomment-5881954336).
No repository issue existed at lookup; no NRA source edits made. Full per-finding
`certification` labels are not global coverage or a zero-debt certificate.

## Provenance and unsuccessful attempts

The canonical project checkout was found to be older52fe8b4. Its diagnostics
are separated under `older-checkpoint/`; none supports current closure. Its
Python3.11 parser also refused Toad's PEP695 declaration. Current installed
Python3.14 and main ab85aa0 complete that same syntax/context. Default NRA's
20s deadline produced explicit incomplete payloads; reruns used150s internal
and165s shell limits. The first unsupported `--workers` invocation did not scan.
No empty/timed-out receipt is called complete.

Large full-presentation JSON receipts are gzip-compressed without changing their
contents (`core-full.json.gz`); raw paired/inventory/summary JSON stays directly
readable. Removed only empty stderr files. Total retained audit receipts are
about2 MB; no new scan cache or generated checkout is retained.

Reproduce current core CLI with `nominal-refactor-advisor src/agent_comms
--context-root src/agent_comms --json --raw-findings --json-payload full
--no-cache --analysis-workers 1 --parse-workers 1 --scan-budget-seconds 150`.
Run `inventory.py` and `paired_raw.py` with `/usr/bin/python`; the latter uses
the exact immutable context paths in the manifest. Global CLI uses those three
context roots and `--json-payload loop`, same other flags.

No tests were added/deleted; this requested audit reuses executed acceptance.
CI is deferred. See `REQUIREMENTS.md` for the reconciled remaining obligations.
