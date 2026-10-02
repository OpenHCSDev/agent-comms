# Selected owner tool on a private N/K route

A newly staged private route initializes the claim protocol before its first
message. Its owner worker receives a nominal `SelectedToolIntent`, so an
engaged FULL turn may request one `selected_claimed_write` call. Triage, legacy
workers, and older private roots without the claim protocol retain no tools.

The model can request a relative existing file and at most 128 KiB of UTF-8
replacement text. The owner verifies the exact selected input, active turn,
claim and resource, consumes a durable one-use slot, and performs the guarded
write. The copied Pi build exposes only this model-callable tool in selected
mode. There is no shell, read, create, or general editing tool. A failed or
uncertain effect is never retried automatically.

An ACP controller's exact operator file plan takes precedence for its selected
claim; that turn offers no model selected tool. Explicitly passing both modes
to the runner remains invalid.

The combined path was exercised with a real copied Pi/provider turn in a
disposable private root: a USER message selected the owner, Pi called the
tool, the guarded owner writer changed `notes.txt` from `before` to `after`,
and the reply `Done` was published. Provider-free tests cover rejection,
one-use ledger, terminal receipt, and default no-tools behavior. The current
installed private route predates the claim protocol; it requires a new staged
root and installed runtime before this tool is available there.
