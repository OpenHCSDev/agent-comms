"""Run P753's failed stopped publication and resume on a bounded real bus copy."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import uuid4
from unittest.mock import patch

from agent_comms.active_route import ActiveRoute, publish_active_route
from agent_comms.check_root import CheckRoot
from agent_comms.comms import Comms
from agent_comms.launch_runtime import LaunchRuntime
from agent_comms.owner_restart import StoppedOwnerFailure
from agent_comms.harness_install import replace_link
from cutover.backup import StoreBackup
from cutover.carry import CarryChain, CarryLedger, PendingConversion, PreparedCarries
from install_gate import prepare_runtime_home
from publish_runtime import HeldRuntimePublication, PublicationIntent, RuntimePublication
from upgrade_bus import Stack


if __name__ == '__main__':
    main()
