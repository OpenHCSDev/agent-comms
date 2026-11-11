"""Pi's extension UI requests dispatch on their declared method and share the dialog replies."""

from agent_comms.execution.pi_events import (
    ConfirmedUiChoice,
    ConfirmUiRequest,
    ExtensionUiRequest,
    PiEvent,
    ValueUiChoice,
)


def test_a_confirm_request_dispatches_on_its_method_and_answers_with_the_shared_replies():
    wire = {
        "type": "extension_ui_request",
        "method": "confirm",
        "id": "r1",
        "title": "Review",
        "message": "Once",
    }
    request = PiEvent.from_wire(wire)
    assert type(request) is ConfirmUiRequest
    assert request.to_wire() == wire
    assert ConfirmedUiChoice(True).response(request).confirmed is True
    assert ValueUiChoice("unrelated").response(request).cancelled is True
    assert request.permission("turn")[0].title == "Review"


def test_an_undeclared_method_stays_the_bare_request_without_reply_authority():
    request = PiEvent.from_wire({"type": "extension_ui_request", "method": "nope", "id": "r2"})
    assert type(request) is ExtensionUiRequest
