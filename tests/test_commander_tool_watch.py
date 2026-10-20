"""The watcher decodes each Pi session line through Core's native entry family once.

Edits are derived from native turn calls, not journaled by the watcher (7e8a0ef8d)."""


from agent_comms.native_tool_call import CodingCall


def test_coordination_call_digests_its_arguments():
    call = CodingCall.decode('coordination-1', 'comms_threads', {})
    assert call.digest() == 'comms_threads {}'


