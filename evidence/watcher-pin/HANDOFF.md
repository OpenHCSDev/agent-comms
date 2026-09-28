# Latest fork pins and watcher shutdown — installed

Core main3aefef1, Toad maincc2d35c (includes109/111), Textual16ede00.
The generated stack lock resolves all three exact fork commits. Fresh full
runtime installed under ~/.local/share/agent-comms/runtime-watcher-20260928;
normal launchers atomically select it. Current bus/native package unchanged.
Existing workers retain the same compaction implementation, so no redundant
owner restart or user-input replay occurred. Open UI processes load new code
when reopened.

Actual installed Toad mounted /home/ts/.agent-comms, began native recursive watch,
reused the watcher, and closed with watcher/child/interpreter joined: exit0.
Toad109 also passed an actual200-file burst during readiness: no concurrent
pipe writers, readiness and later delivery observed, clean exit. All fixture
files stayed in owned ~/wt temporary directories and were cleaned.

This fixes recursive watcher startup/shutdown. The separate core unread-history
scan still affects the complete archived-history shutdown probe; Cicero owns
that correction. Full nominal refactor229/Toad107 and native history243/244
remain independent active work. No full-suite or paid-provider claim here.
