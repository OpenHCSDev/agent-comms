# Preserve reviewed source mode at atomic replacement

Existing `_atomic_write_text` owns tempfile creation, fsync and atomic replacement. Private callers retain the current0600 default. Existing GlobalSourceInstall supplies installation mode through its behavior hook; ReplaceGlobalSource derives it from its reviewed original file. Set the opened temporary descriptor mode BEFORE replacement. No post-publication chmod, duplicate atomic writer, installer class, environment or native build.

Source inspection and existing refactor-audit AST enumerate the shared primitive, original callers and global source behavior. Implement the related owner/caller change, then one final batched mode/byte sanity: executable755 and regular644 preserved, private default600 unchanged, file mode already correct at replacement, changed original refused with preserved preimage. Actual public deployment stays with the parent stopped-owner publication; current334 and original failed337 remain untouched.

## Ready source and concrete result

Core production34b7932b uses portable chmod on the original temporary path before writing/fsync/replacement. The existing PrivateFileRole.permissions is the sole private-file mode declaration; both the writer default and GlobalSourceInstall derive it. ReplaceGlobalSource derives replacement permissions from its reviewed original. No added class or second atomic writer.

The existing refactor-audit parser covered413 modules with zero omissions; current changed owner source and its original PrivateFileRole declaration are retained in after-ast.json. Dynamic/external resolution remains an explicit static limit. Existing callers keep the same private default.

One initial installed992 actual-files batch and the final corrected source-owner batch preserved755 executable and644 ordinary modes, exact new bytes/original preimages,600 private/new-source defaults, and refusal on changed originals, with no remaining tempfiles. The final source batch is not labelled a final installed-wheel check: that tiny matched-mode import check follows normal merged550 receiver pins. No provider, UI rerun, native rebuild, global write or failed337 retry occurred. The parent-granted source helper modification is preserved in evidence, remains in its original tracking checkout, and is not silently committed with unrelated parent changes.
