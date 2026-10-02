# Preserve reviewed source mode at atomic replacement

Existing `_atomic_write_text` owns tempfile creation, fsync and atomic replacement. Private callers retain the current0600 default. Existing GlobalSourceInstall supplies installation mode through its behavior hook; ReplaceGlobalSource derives it from its reviewed original file. Set the opened temporary descriptor mode BEFORE replacement. No post-publication chmod, duplicate atomic writer, installer class, environment or native build.

Source inspection and existing refactor-audit AST enumerate the shared primitive, original callers and global source behavior. Implement the related owner/caller change, then one final batched mode/byte sanity: executable755 and regular644 preserved, private default600 unchanged, file mode already correct at replacement, changed original refused with preserved preimage. Actual public deployment stays with the parent stopped-owner publication; current334 and original failed337 remain untouched.
