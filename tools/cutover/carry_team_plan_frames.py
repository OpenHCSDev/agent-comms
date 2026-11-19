"""Move the plan phases stored in each HUD frame into the one team plan file.

Phase readiness is a team fact the heartbeat now writes once, to ``team_plan.json``. Frames
written before that still hold their own copy under ``phases``, which the frame decoder
refuses as an unknown field, so every board read would fail until the first beat rewrote
them. The first frame's copy becomes the team plan, every frame's copy is dropped, and the
first read after installation shows the same phases as before. Delete this step after the
live installation.
"""

from __future__ import annotations

import json
from typing import ClassVar

from cutover.carry_shared_team_frames import FlatTeamFrameFile, SharedTeamFrames

from agent_comms.installer.probes.carry import Carried, Carry
from agent_comms.storage.locked_store import RootFile
from agent_comms.storage.store_files import AtomicFile
