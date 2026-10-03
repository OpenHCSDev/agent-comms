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
FD is held across external native consumption; Darwin /dev/fd path is scoped to
the calling process. Final changed long-root installed resource/native controls
come last; original575 NotSent inputs are never reused.
