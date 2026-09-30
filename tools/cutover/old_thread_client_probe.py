"""Authentic old-client history read on an isolated, retired batch copy only."""
import json
from pathlib import Path
import sys

from agent_comms.comms import Comms
from agent_comms.acp import CommsAgent
from agent_comms.config_options import ThinkingLevelConfigOption
from agent_comms.errors import RelationViolationError


def main():
    root = Path(sys.argv[1])
    service = Comms(root)
    assert all(not thread.process_alive for thread in service.registry.all_threads().values())
    registry = root / 'registry.json'
    before = json.loads(registry.read_text())
    assert all('last_goal_report_turn' not in row for row in before['threads'].values())
    closed = service.owners.maintenance.read()
    assert closed is not None and closed.phase == 'paused'
    try:
        with service.owners.maintenance.admit_ingress():
            raise AssertionError('Paused fixture admitted ingress')
    except RelationViolationError:
        pass

    # The real UI-facing history API calls Messaging.user_identity. With no
    # prior USER, that registers a non-executable record without turn admission.
    service.views.dm_display_page('phase-alpha', worktree=str(root), limit=1)
    # The ACP configuration owner has another ordinary metadata writer. No
    # backend, session attachment or native operation is started by this call.
    client = CommsAgent(service)
    config = client.sessions.config
    option = config.catalog_for(ThinkingLevelConfigOption)
    option.persist(config, service.registry.require('phase-alpha'), 'high')
    assert option.current_value(service.registry.require('phase-alpha')) == 'high'
    after = json.loads(registry.read_text())
    reintroduced = [name for name, row in after['threads'].items()
                    if 'last_goal_report_turn' in row]
    assert set(reintroduced) == set(after['threads'])
    assert 'user' not in before['threads'] and after['threads']['user']['role'] == 'user'
    with service.registry.store.locked():
        service.registry.store.private_guard_unlocked().verify()
    assert service.owners.maintenance.read() == closed
    print(json.dumps({'client_operation': 'HistoryViews.dm_display_page',
                      'history_read_returned': True, 'maintenance_phase': 'paused',
                      'ingress_refused': True, 'user_identity_created': True,
                      'metadata_operation': 'ThinkingLevelConfigOption.persist',
                      'metadata_write_while_paused': True,
                      'retired_field_reintroduced_records': len(reintroduced),
                      'original_private_guard_accepts_write': True,
                      'provider_calls': 0, 'prompt_count': 0}), flush=True)


if __name__ == '__main__':
    main()
