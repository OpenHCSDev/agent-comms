## P78 — retained, measured Toad row views

Core changes are **zero**. The rejected rich/Core width edits were reverted completely, including slots changes. This checkpoint is Toad-only.

The existing ThreadComms row map now retains immutable ThreadRowView values; unchanged person+canonical unread reuses the exact previous view and Core model. EntryView owns cached width of immutable relationship entries. GroupView retains those entry views in the existing native group's presentation lifetime; there is no text-keyed width map or additional persistent row registry. Widths are consumed through the existing preparation owner, not measured on the UI thread. Group/row maxima replace all history-wide measuring loops. The one remaining cell_length in thread_comms.py is the constant-size context heading.

The existing source abstraction now supplies typed waiting/unconnected/live behavior instead of Protocol-only declarations and literal status branches in the panel. Reads use the existing preparation owner.

Base ea22fac108b408fd1950c5ca4b14d7a1b4f9b222; checkpoint ffdf4899. **src: 2 files +112/-49 = +63**, Core0, tests0. This exceeds the standing +40 review threshold: draft explicitly requires ownership/size review, not acceptance. No source relocation credit.

Source-first NRA census: 1,064 Core/Toad/dependency modules, zero parse omissions, complete related declaration/import/consumer/decision sites retained in Core checkout .scratch/p78/before.json. Arbitrary dynamic aliases are not established by AST syntax.

Paired runtime imports, F401/F821 (Python3.12 target), whitespace pass. Required cell_length lexical count in thread_comms.py: **1**. No Toad tests added/run. Real plain-st/Xvfb one-DM before/after measurements and installed acceptance remain in progress; do not claim target row count or UI acceptance from this draft.

No Core dependency, installed package, shared pointer or saved-session changes. New disposable UI evidence will stay under owned persistent .scratch/p78 and be retired after acceptance. P108 will follow on this branch after P78, not as a competing animation mechanism.
