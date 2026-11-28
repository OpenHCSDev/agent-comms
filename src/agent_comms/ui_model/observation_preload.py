"""Preload target for the connector's observation forkserver.

``multiprocessing.forkserver`` warms itself solely by importing module names; this module
imports ``observation_service`` and builds its declaration and table-schema caches once
before any ``/observe`` child forks.
"""

from agent_comms.ui_model.observation_service import warm_observation_forkserver

warm_observation_forkserver()
