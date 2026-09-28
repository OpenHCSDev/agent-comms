# Channel notification feedback

Actual user channel messages63/64 were processed: both running subscribers'
native assistant transcripts contain IGNORE. The missing feature was feedback,
not model receipt. Stopped recipients retain pending assignments.

The existing assignment lifecycle now owns presentation labels. HistoryViews
reads one visible message window from existing coordinator rows without writing,
creating a second status ledger, or scheduling work. In-flight labels additionally
require a matching live owner turn; reserved unknown inputs alone cannot animate.

SelectedExecution previously leased turns directly without updating the normal
Activity log. It now reports checking/responding through AgentActivity and uses
that same owner's finish_turn to release/clear activity. Toad consumes ordinary
thread status plus per-message notification results.

Actual live source-path probe65 observed Pending→Checking→Responding→Responded
for the Comms UX owner, and Checking→Checked/no-response for PR95. Receipt in
live_channel_observation.json. This alone does not prove mounted installed UI.

Supplementary focused tests:3 passed. The focused command's global85% coverage
gate failed because only3 tests ran (32.94% total); not a broad-suite pass.
CI deferred. Real installed UI/live path validation follows separately.
