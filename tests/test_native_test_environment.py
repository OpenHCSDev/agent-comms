from importlib.metadata import distribution
from pathlib import Path

from agent_comms import native_package


def test_gate_takes_its_own_pins_artifact_not_the_installed_one(native_runtime_package, pytestconfig):
    """The installed package may carry another pin; the gate's artifact is the checkout's own."""
    installed = Path(distribution("agent-comms").locate_file("agent_comms/_native")).resolve()
    environment = native_package.native_environment(pytestconfig.getoption("native_cache"))
    assert environment.name == native_package.pin_id()
    assert native_runtime_package.path.is_relative_to(environment)
    assert not native_runtime_package.path.resolve().is_relative_to(installed)
