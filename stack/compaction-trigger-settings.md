# Read-only effective Pi compaction decision (dormant seam)

`owner_compaction_settings.read_compaction_decision` evaluates Pi's own
`shouldCompact` against caller-supplied selected-model context tokens/window
and the effective `SettingsManager` compaction settings. It runs only from a
verified disposable native package under its import fence and `PI_OFFLINE=1`;
no provider/model request, session writer, ACP trigger, or owner claim occurs.
The caller-provided model/window is **evidence**, not an authority grant.

The usual Pi `FileSettingsStorage` can take/write lock files even for reads.
This reader supplies a bounded read-only `SettingsManager.fromStorage` adapter
instead: it loads global and project settings from regular single-link files,
checks each stat before and after reading, refuses parse errors and any
storage callback that tries to write, and uses Pi's own merge/default/migration
logic. Absent files use Pi defaults; malformed, oversized, symlink or changing
files refuse before `shouldCompact`. The result is only booleans and integer
settings—no config content, prompt, summary or credentials. Provider-free
controls use a private `PI_CODING_AGENT_DIR`, exercise global/project precedence,
strict trigger boundary, disable and malformed refusal, no lock-file/session
mutation, and preflight invalid-input refusal without launching Pi.

**Not production wired.** Before connecting this to ACP, bind the context
window to the verified selected model (not a client-supplied number), pass the
same effective keep-recent setting into the native preparer and provider
summary strategy, and capture/recheck model, settings revision, ingress and
turn before any paid work and native commit. A skip cannot waive the existing
hard-context backstop. Existing `prepare_native_source` still uses Pi's
compiled default recent window unless its explicit test override is supplied;
it must **not** be silently paired with a different effective window. No
provider testing or activation is authorized by this seam.
