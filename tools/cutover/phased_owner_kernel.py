"""Load the ONE current restart phase under authentic original declarations.

Only the new resource phase module is loaded. Registry, Thread, FieldCodec and
the original process lifecycle remain the original installed declarations.
This outside-src tool is retired after the authorized format transition.
"""
import importlib.util
from pathlib import Path
import sys


def load_original_phase(package: Path):
    # Import original lifecycle first. It owns original Thread decoding, stop
    # custody and release receipts throughout admission and retirement.
    import agent_comms.owner_lifecycle

    for name in ('owner_restart', 'owner_cutover'):
        qualified = f'agent_comms.{name}'
        specification = importlib.util.spec_from_file_location(qualified, package / f'{name}.py')
        module = importlib.util.module_from_spec(specification)
        sys.modules[qualified] = module
        specification.loader.exec_module(module)

