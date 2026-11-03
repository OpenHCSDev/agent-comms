"""A real observation pass over the encoded channel a connector would carry."""

import socket

from agent_comms.repo import Checkout
from agent_comms.ui_model.observation import (
    ObservationService,
    ObserveSidebar,
    ServiceEnd,
    SidebarResult,
    ViewerEnd,
)


async def test_sidebar_interest_and_its_answer_cross_the_encoded_channel(comms):
    service_socket, viewer_socket = socket.socketpair()
    with ServiceEnd(service_socket.detach()) as service_end, ViewerEnd(viewer_socket.detach()) as viewer:
        service = ObservationService(comms, service_end)
        wanted = ObserveSidebar(Checkout(comms.root.parent), True, True)
        try:
            viewer.send(wanted)
            service.receive()
            await service.observe(set())
            answers = []
            while viewer.poll():
                answers.append(viewer.recv())
            sidebar = [answer for answer in answers if isinstance(answer, SidebarResult)]
            assert sidebar and sidebar[-1].request == wanted
        finally:
            service.retire()
            comms.views.transcript_reads.close()
