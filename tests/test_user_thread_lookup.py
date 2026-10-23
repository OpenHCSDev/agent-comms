"""The stored registry revision owns human identity and UI history uses it."""

from agent_comms.channel_targets import CommandTarget


from dataclasses import replace

from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.registry_document import NoUserThread, RegistryDocument
from agent_comms.thread_identity import UserThreadRole
from agent_comms.thread_text import GivenThreadText
from conftest import installed_comms


def test_user_answer_follows_revisions_and_stays_out_of_wire_fields(tmp_path):
    comms = wire(tmp_path)
    human = comms.messaging.user_identity(str(tmp_path))
    comms.registry.rename(human.name, 'renamed-human')
    assert comms.registry.user_thread().existing()[0].name == 'renamed-human'
    selected = comms.registry.user_thread().existing()[0]
    comms.registry.register(replace(selected, title=GivenThreadText('current human title')))
    assert comms.registry.user_thread().existing()[0].title == GivenThreadText('current human title')
    raw = FieldCodec.encode(comms.registry.store.read())
    assert '_human' not in raw
    assert RegistryDocument.from_wire(raw).user_thread().existing()[0].name == 'renamed-human'
    assert RegistryDocument.entry_from_wire(raw, 'renamed-human').thread.name == 'renamed-human'
    comms.registry.unregister('renamed-human')
    comms.registry.remove('renamed-human')
    assert isinstance(comms.registry.user_thread(), NoUserThread)
    assert comms.messaging.user_identity(str(tmp_path)).incarnation != human.incarnation


def test_channel_and_dm_history_keep_the_human_as_sender(tmp_path):
    comms = installed_comms(tmp_path)
    comms.registry.declare(comms.registry.new_thread('peer', frozenset({'comms'}), str(tmp_path)))
    channel = comms.messaging.send_user_message(CommandTarget.named('#comms'), 'own channel message', worktree=str(tmp_path))
    direct = comms.messaging.send_user_message(CommandTarget.named('peer'), 'own DM message', worktree=str(tmp_path))
    human = comms.registry.user_thread().existing()[0]
    assert channel.sender == direct.sender == human.name
    assert channel.sender_role is direct.sender_role is UserThreadRole
    channel_page = comms.views.channel_display_page(
        '#comms', viewer=comms.views.display_viewer(str(tmp_path)))
    direct_page = comms.views.dm_display_page('peer', worktree=str(tmp_path))
    assert channel.message_id in {message.message_id for message in channel_page.messages}
    assert direct.message_id in {message.message_id for message in direct_page.messages}
    assert all(message.sender == human.name for message in (*channel_page.messages, *direct_page.messages))
