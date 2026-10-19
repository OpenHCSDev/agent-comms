"""The stored registry revision owns human identity and UI history uses it."""

from dataclasses import replace

from agent_comms.comms import Comms
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import FoundUserThread, NoUserThread, RegistryDocument
from agent_comms.thread_identity import AgentThreadRole, UserThreadRole


def test_user_lookup_reads_one_stored_answer_without_a_roster_walk(tmp_path, monkeypatch):
    comms = Comms(tmp_path)
    assert isinstance(comms.registry.user_thread(), NoUserThread)
    comms.registry.declare(comms.registry.new_thread(
        'user', frozenset({'comms'}), str(tmp_path), role=AgentThreadRole))
    human = comms.messaging.user_identity(str(tmp_path))
    assert human.name != 'user' and human.role is UserThreadRole
    assert isinstance(comms.registry.user_thread(), FoundUserThread)

    def forbidden(*args):
        raise AssertionError('The existing human answer walked the registry')

    monkeypatch.setattr(comms.registry, 'all_threads', forbidden)
    monkeypatch.setattr(AgentThreadRole, 'human', classmethod(forbidden))
    monkeypatch.setattr(UserThreadRole, 'human', classmethod(forbidden))
    for _ in range(1000):
        assert comms.messaging.user_identity(str(tmp_path)).incarnation == human.incarnation


def test_user_answer_follows_revisions_and_stays_out_of_wire_fields(tmp_path):
    comms = Comms(tmp_path)
    human = comms.messaging.user_identity(str(tmp_path))
    comms.registry.rename(human.name, 'renamed-human')
    assert comms.registry.user_thread().existing()[0].name == 'renamed-human'
    selected = comms.registry.user_thread().existing()[0]
    comms.registry.register(replace(selected, title='current human title'))
    assert comms.registry.user_thread().existing()[0].title == 'current human title'
    raw = FieldCodec.encode(comms.registry.store.read())
    assert '_human' not in raw
    assert RegistryDocument.from_wire(raw).user_thread().existing()[0].name == 'renamed-human'
    assert RegistryDocument.entry_from_wire(raw, 'renamed-human').thread.name == 'renamed-human'
    comms.registry.unregister('renamed-human')
    comms.registry.remove('renamed-human')
    assert isinstance(comms.registry.user_thread(), NoUserThread)
    assert comms.messaging.user_identity(str(tmp_path)).incarnation != human.incarnation


def test_channel_and_dm_history_keep_the_human_as_sender(tmp_path):
    comms = Comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('peer', frozenset({'comms'}), str(tmp_path)))
    channel = comms.messaging.send_user_message('#comms', 'own channel message', worktree=str(tmp_path))
    direct = comms.messaging.send_user_message('peer', 'own DM message', worktree=str(tmp_path))
    human = comms.registry.user_thread().existing()[0]
    assert channel.sender == direct.sender == human.name
    assert channel.sender_role is direct.sender_role is UserThreadRole
    channel_page = comms.views.channel_display_page(
        '#comms', viewer=comms.views.display_viewer(str(tmp_path)))
    direct_page = comms.views.dm_display_page('peer', worktree=str(tmp_path))
    assert channel.message_id in {message.message_id for message in channel_page.messages}
    assert direct.message_id in {message.message_id for message in direct_page.messages}
    assert all(message.sender == human.name for message in (*channel_page.messages, *direct_page.messages))
