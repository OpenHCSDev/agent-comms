# Assigned channel chat cutover

The stack pins merged core `5f4db14a`, merged Toad `bc503af8` (#190), and
Textual `609b74bf`. The installed candidate
`runtime-assigned-chat-20260929` is noneditable; each distribution's
`direct_url.json` names the exact fork commit in `stack/pyproject.toml`.

Actual installed acceptance passed with the canonical selected Pi package:
an agent CLI sender posted into `#team`, a distinct recipient entered its
native ACP turn, Toad painted the original inbound message in the recipient's
chat, the handling state settled to the core's recorded decision, and only
one block with that wire sequence remained. One selected Pi request occurred;
the fixture used its private wire and controlled localhost provider.

A separate read-only App/Pilot probe of the user's live saved `#nra` bus
painted post 189 ("testing in channel directly") and
`Handling: Checked — no response` in nra-architecture's chat. The bus size
was unchanged and the App reported no exception. No original input was
replayed or acknowledged by this check.

This fixes the recent assigned messages missing from recipient chat. Core
currently projects at most five recent assignments; complete older/unread
history remains with PR389. CI is deferred by owner instruction. No claim
is made for the separate warm DM tab-return performance issue.
