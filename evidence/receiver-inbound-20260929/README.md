# Agent-channel receiver checkpoint

Pins merged agent-comms `5f4db14a`, Toad `b30e41b2` (PR188 visibility plus
PR189 agent-sender regression), and unchanged Textual `609b74bf` in the stack.
The candidate `runtime-receiver-inbound-20260929` is a noneditable installation
from the locked fork sources. Its installed distribution metadata names these
exact commits.

The maintained `tests/receiver_inbound_installed_pilot.py` passed using this
candidate, the canonical Pi package and a controlled localhost provider. A
registered agent used the actual CLI to send into `#team`; the recipient's
selected Pi process reached the provider once, and its open Toad agent tab
painted `Latest inbound #team from @sender` while the turn was running. Expanded
details held the original message body. The test used a disposable private
wire, stopped its owner, and did not mutate the user's bus or replay an input.

The earlier read-only installed UI probe on the user's saved `#nra` exchange
confirmed that the receiver's channel view retains the original inbound row.
The old native tab summarized recent assignments as only `5 recent`. This
checkpoint makes the latest assigned sender/channel visible in the collapsed
agent tab. It does not implement a durable per-recipient unread ledger or a
complete native-tab inbox for older assignments; channel history retains them.

The existing open Toad GUI runs an older installation and must be closed and
reopened to load this UI. No CI or final latency claim is made; CI is deferred
by owner direction. The larger multi-owner user journey was not rerun because
the resource headroom check warns; the focused full user path used one owner.
