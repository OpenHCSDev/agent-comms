"""A new extension UI request declares itself and inherits the shared reply behaviour."""

from agent_comms.pi_events import ConfirmUiRequest, ConfirmedUiChoice, PiEvent, ValueUiChoice


def test_extension_ui_new_case_uses_declared_dispatch_and_shared_reply_behavior():
    class ReviewUiRequest(ConfirmUiRequest):
        method = "reviewFixture"

    try:
        wire = {
            "type": "extension_ui_request",
            "method": "reviewFixture",
            "id": "r1",
            "title": "Review",
            "message": "Once",
        }
        request = PiEvent.from_wire(wire)
        assert type(request) is ReviewUiRequest
        assert request.to_wire() == wire
        assert ConfirmedUiChoice(True).response(request).confirmed is True
        assert ValueUiChoice("unrelated").response(request).cancelled is True
        assert request.permission("turn")[0].title == "Review"
    finally:
        del PiEvent.__registry__[ReviewUiRequest.declared_name]
