# Retained history must actually paint

User reported Saved history available with an empty conversation. The previous
UI check proved loaded widgets and cleared initial loading, not visible messages.

Toad137 removes Widget from nominal ConversationBlock. Textual inherits CSS along
the first DOM base; the marker prematurely selected Widget instead of the actual
VerticalGroup/StreamingMarkdown base. No duplicated CSS or compatibility layer.

`live_paint.py` attaches actual installed Toad to retained live sessions without
sending a prompt. It crops rendered compositor strips to the conversation and
compares five-word phrases with the loaded user/agent transcript.

- Old installed NRA: height0, text0, zero matching phrases (reproduced failure).
- Fixed installed NRA: height57,911 nonspace characters,110 matching phrases.
- Fixed installed PR95: height48,679 characters,105 matching phrases.
- Final fork merge installed: Toad5983adba; coree6fa8feb and Textualc9743801 unchanged.
- Five launchers activated to immutable runtime-history-visible-20260928.
- No owner restart, native-session change, store reset or input replay.

Only one production declaration changes (one line removed and replaced). The
regression measures actual painting; prior widget-only receipt is superseded for
visible-history readiness. Existing Toad must reopen to load corrected classes.
