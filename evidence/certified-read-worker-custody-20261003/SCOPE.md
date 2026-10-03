# Certified read custody ends before result delivery

Mendel integration; Arendt560 preserves the original still-live execution.
Base85abe4276e91d52a72eec7411ed2314aaccc8c02, reused isolated checkout.

Original privatePID1583616 completed native finalb7807f3e. Kernel shows
BUS inode17965242 heldEX fd11 and waitingEX fd10 in that same process.
Original stack: TurnProgress.publish_result339 -> transcript_checkpoint490 ->
AssignedTranscriptSource.frontier/window/rows -> WireLog.conversation_sources112.
Root/proof belong to Arendt's existing s4-three-cuts-configured01; no replay,
restart, signals, provider call or source/history mutation here.

WireLog.read_certified_async opens/acquires the original descriptor on the
event loop, consumes certification in its joined worker, then closes the file
only after that worker result resumes on the loop. POSIX release_store_lock
intentionally leaves inherited custody until last close. The idle worker can
therefore finish without releasing the original bus lock. Synchronous final
publication blocks the loop acquiring the same inode; original close cannot run.

Own the existing read-resource transfer and closure family. Consume and close
the original acquired lock file in the worker before its detached result is
returned. Keep cancelled acquisition/consumption joined and certificates exact.
Read all source/consumer lifetimes before editing. No reentrant ambient registry,
shared-lock workaround, timeout padding, copied source or pending-state mirror.
Public root remains untouched. Original completed answer and uncertain inputs
remain intact. Source implementation first, proportionate changed custody checks
and installed original-state observation last; no second provider reproduction.
