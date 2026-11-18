"""The older reader's half of tools/old_reader_decodes_files.py: run by an interpreter whose
installed agent_comms predates message files, it decodes the records on stdin with that
installed codec and prints the file it decoded with, then the bodies it read.

It imports only declarations an installation before message files already has.
"""

from __future__ import annotations

import json
import sys

import agent_comms
from agent_comms.messaging.history_result import ChannelHistoryPageResult
from agent_comms.nominal.field_codec import FieldCodec
from agent_comms.transport.tools.inbox import CommsInboxTool
from agent_comms.transport.tools.sidebar import CommsConversationPageTool, CommsUserSendTool


def main() -> None:
    page, history, inbox, request = json.load(sys.stdin)
    decoded_page = FieldCodec.decode(CommsConversationPageTool.result_type(), page)
    decoded_history = FieldCodec.decode(ChannelHistoryPageResult, history)
    decoded_inbox = FieldCodec.decode(CommsInboxTool.result_type(), inbox)
    print(
        json.dumps(
            [
                agent_comms.__file__,
                [message.body for message in decoded_page.messages],
                [message.body for message in decoded_history.messages],
                [message.body for message in decoded_inbox.messages],
                FieldCodec.decode(CommsUserSendTool, request).body,
            ]
        )
    )


if __name__ == "__main__":
    main()
