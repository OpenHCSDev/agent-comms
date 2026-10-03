# Private Unix address custody

Owner: existing PrivateSocketRole. Original filesystem path remains socket and
receipt identity; descriptor-relative address is an acquired directory resource,
not another semantic store. OwnerToolSocket holds its directory FD until native
transport/handlers retire. Runtime RPC and recovery bind/connect use the same
owner. Delete temp-hash fallback, short-name assumption and gateway length refusal.
Keep original private-directory checks, same-UID/native-PID token authentication,
stale endpoint identity checks, and unchanged tool ledgers. No native edit.

AST before/after includes all src/tests/tools. Native shipped .mjs clients consume
an address from the existing environment without interpreting filesystem layout.
Patterns IDEN-1 (socket address used as evidence directory), IMPL-13 (three address
policies), IMPL-12 (repeated transport boundary decisions). Linux proc directory
FD is held across external native consumption. Other POSIX systems borrow a
short private temporary directory link to the original parent; socket/proof files
remain original. This portability branch is a filesystem address resource, not
a semantic reader alias; actual macOS execution is not claimed. Native project
observation holds its address in PiSessionChild until retirement, including
prepared/retained children. Launch configuration/key keeps its original path. Final changed long-root installed resource/native controls
come last; original575 NotSent inputs are never reused.
