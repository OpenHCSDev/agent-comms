

from agent_comms.acp import CommsAgent
from agent_comms.comms import wire
from agent_comms.native_arguments import GivenBinary
from acp.schema import AuthCapabilities, ClientCapabilities


async def test_terminal_auth_is_only_advertised_when_supported(tmp_path):
    agent = CommsAgent(wire(tmp_path), agent_bin=GivenBinary("pi"))
    assert not (await agent.initialize(1, ClientCapabilities())).auth_methods
    methods = (await agent.initialize(
        1, ClientCapabilities(auth=AuthCapabilities(terminal=True))
    )).auth_methods
    method, = methods
    assert method.id == "pi-login"
    assert method.model_dump(by_alias=True)["type"] == "terminal"
    assert method.args == ["--login"]


