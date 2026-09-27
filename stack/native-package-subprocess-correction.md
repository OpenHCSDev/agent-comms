# Package subprocess correction — successor to NON-CLEAN 8c6ce7

## Finding and preserved baseline

Both designated reviewers now retain **NON-CLEAN** for the complete `8c6ce7`
import/package freeze. Preflight independently reproduced an installed-but-
unapproved npm source executing configured `npmCommand view` through
`DefaultPackageManager.checkForAvailableUpdates()`. The separately guarded actual
update refused the same source. Sink withdrew its initial broader package-source
CLEAN wording; its import/approved-entry/helper positives remain bounded evidence.

This is a reproduced shipped package-manager API admission defect and an
identified interactive update-check call site, **not a demonstrated canonical
interactive-init bypass**: earlier resource resolution can reject that source.
The owner also reproduced the corresponding git `rev-parse`/`ls-remote` path using
fake local commands. Old tree `b6d13d86…`, build `26e29f3669b35ce5`, package manager
`dc6f9123…` and all reviewer/owner artifacts remain untouched.

## Correction and boundary

The entire raw configured-source set is admitted before deduplication, probe
construction or concurrent update-check scheduling. Pinned, filtered and project
sources do not bypass this check. Only exact manifest-local prebuilt packages are
admitted; npm/git acquisition is intentionally unsupported by this deployment.

Source admission alone is not enough: metadata/probe APIs are ordinary exported
class methods. The exact upstream package-manager transform therefore also
unconditionally rejects **all three actual package-manager subprocess sinks**:
`spawnCommand`, `spawnCaptureCommand`, `runCommandSync`. The transform asserts the
inventory of two `spawnProcess` calls and one `spawnProcessSync` call against an
exact upstream SHA. This also covers direct npm version checks, git metadata and
remote checks, legacy global-root discovery and npm/pnpm/bun command variants.

There is no caller-selectable approval token or command override to bypass the
sink guard. Approved local package installation, discovery and update/status
checks require no subprocess and remain supported. Some upstream best-effort
metadata methods catch refusal and return false/undefined; tests require zero
command execution rather than falsely claiming every public method throws.

This policy applies to the SDK **package manager**, not all OS subprocesses.
Trusted tools and explicitly approved MCP servers retain their separate authority
and are not turned into an OS sandbox by the import hook.

## Exact successor and evidence

- Full tree: `628b68df3c1cc91e6b6698eb639ff108b4835d027cc34a219087861664d01a07`.
- Manifest: `e9f19aa7add2a71c917f2a27b5b721e2a2a9de29157152b6bd6cfa03dab6476a`.
- Build: `e9f19aa7add2a71c`.
- Package manager: `103067298d031fcc2000962b15ec751746e93add4350befbd4cee91c9fafc42e`.
- Fence: `18a47064c28cce5f64233493d49c0a683e7bf29cdbb00f9ae492f36e32fd6251`.
- Manager `10ac30c1…`, extension loader `5aa2488a…` and helper `7f81d710…` unchanged.

Fresh canonical pointer `/var/tmp/pr95-update-canonical-package`; repository
pointer `/var/tmp/pr95-update-canonical-stage` selects
`/var/tmp/pr95-update-canonical-VVhS8l`.

`test-native-package-processes.py PACKAGE [old|new]` runs the committed JS fixture
via eval with the actual pinned fence, disposable HOME and kernel network denial.
`PI_OFFLINE=0`. No provider credentials or network probes. Two counterfactual
fake-command controls verify both npm and git markers can execute. Old mode
reproduces both update-check escapes and guarded update denials; strict new mode
fails on the old artifact with `package subprocess executed`.

New mode passes **36 cases**: 16 configured-source denials across npm/git, pinned,
absent, user/project and filtered/plain cases; one approved-source aggregate
(install, relative discovery, check, update); 19 direct metadata/probe/async/sync
and npm/pnpm/bun sink controls. Project trust is an explicit in-memory test double
so those cases reach package admission; no persisted trust/approval is written.
The initial fixture stopped at the separate project-trust guard; its failure log
is preserved, not relabeled as package-boundary proof.

The exact new canonical CLI also passes real in-root PR77 install/RPC commands/
status with no unapproved child, and the reached committed ws ancestor negative.
The RPC fixture no longer attempts a loopback/socket negative-control syscall;
it retains kernel denial but makes no network probes. The historical SIGSYS
control belongs only to the preserved earlier report. PR77 startup itself still
is NOT claimed to reach ws.

Fresh canonical preparation and repeated verification pass. All nine native/
import scripts and adaptive contracts pass. Source MCP/provenance/authority/
journal/ingress: **243 passed, 1 skipped**; offline sdist → extracted-wheel bridge:
**54 passed**. Logs `/var/tmp/pr95-update-*.log`, including old controls, strict old
failure, canonical processes/RPC and wheel/source results. Quality checks cover
Black, Ruff, mypy, shell syntax and deterministic transform bytes.

Fresh exact-byte review is required from BOTH designated reviewers. The two
narrow `3585b0f` writer-retirement CLEAN verdicts remain distinct. Runtime/idle
manager disposal and explicit recovery, adaptive pre-summary capture, ACP/
correction/send admission, keyed metadata-only outbox, E2E and final integrated
review are still open. No activation, installed/live-session modification or
UNKNOWN replay is authorized by this checkpoint.
