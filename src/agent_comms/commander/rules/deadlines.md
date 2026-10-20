---
name: deadlines
description: "15 minutes is the ceiling; count the work and never pad deadlines or reschedule stalling; deadlines prevent stalling, never punish initiative deleting real slop"
type: feedback
---

**Why:** the owner reads a due time as an ETA; a padded one says the commander does not know what the work is, and a default deadline lets units spend slack the work does not need.

Count the steps, then set `date -d '+N min'`: about 2 minutes for a few shell commands (removing checkouts, closing PRs, pushing a branch), 5 for a merge or publish, about 8 for a qualified build plus one store step plus a DM. Never type a round clock time. No investigation runs longer than 5 minutes. Pull a deadline in when the commit lands early.

Tristan, 2026-10-20: "less than an hour, 15 minutes"; 00:42 "whys it due in 15 minutes? why 15 minutes does it need 15 minutes?"; 04:24 "deleting 2 checkouts doesn't take 15 minutes".

**Deadlines stop stalling; they never punish slop deletion.** A unit late because it folded in deleting real slop on its surface is credited for the initiative, not struck. Lateness from stalling, ceremony or wrong work is still a strike, never hidden by rescheduling.

Tristan, 2026-10-20 11:53: "the deadlines are mostly to prevent stalling and put pressure on agents to be competent ... we allow it and reward the unit for taking that initiative".
