"""Declared module deletions reach the real commander CLI and saved order codec."""
from agent_comms.repository_name import RepositoryName
import json
import os
import subprocess
import sys
from pathlib import Path

from agent_comms.change_report import FileLine
from agent_comms.commander.config import Repo
from agent_comms.field_codec import FieldCodec
from agent_comms.todos import ModuleDeletion, NoOrderDeletion
from test_commander_problem_orders import order, order_body, problem


def test_file_line_scan_keeps_the_absolute_path():
    assert FileLine.scan('/home/ts/a.py:1') == (FileLine('/home/ts/a.py', 1),)


def test_module_deletion_keeps_existing_orders_and_carries_module_instructions(commander):
    comms, _ = commander
    original = order(comms.root, 'unchanged')
    encoded = FieldCodec.encode(original)
    assert 'deletes' not in encoded
    assert FieldCodec.decode(type(original), encoded) == original
    assert original.deletes == NoOrderDeletion()
    assert original.deletes.modules == ()
    deletion = ModuleDeletion(('src/agent_comms/acp_ingress.py',))
    assert FieldCodec.decode(type(deletion), FieldCodec.encode(deletion)) == deletion
    assert deletion.instructions() == ('Deletes: src/agent_comms/acp_ingress.py',)


def test_mentioning_tools_without_a_file_reference_does_not_declare_a_tool_edit(
        commander, commander_script):
    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    edit = 'Change the counter; tools/ is unrelated to this edit.'
    problem(script, order_body(repo, edit=edit))
    assert comms.goals.todos.get('P1').order.edits == (edit,)


def test_order_send_cli_refuses_declared_module_imported_by_toad(
        commander, commander_script, tmp_path):
    comms, _ = commander
    script, repo = commander_script
    (repo / 'count.txt').write_text('0\n')
    problem(script, order_body(repo))
    (repo / 'acp_ingress.py').write_text('class Ingress: pass\n')
    toad = tmp_path / 'toad'
    (toad / 'src').mkdir(parents=True)
    (toad / 'src' / 'app.py').write_text('from agent_comms.acp_ingress import Ingress\n')
    script = script.model_copy(update={'repos': (Repo(checkout=toad, github=RepositoryName.from_wire('OpenHCSDev/toad')),)})
    script.write(tmp_path)
    body = Path(order_body(repo, edit='Remove the obsolete ingress implementation.',
                           deletes=ModuleDeletion(('src/agent_comms/acp_ingress.py',))))
    document = json.loads(body.read_text())
    document['problems'] = ['P1']
    body.write_text(json.dumps(document))
    before = comms.goals.todos.get('P1')
    command = [str(Path(sys.executable).with_name('agent-comms-commander')),
               '--root', str(comms.root), 'order', 'send', 'worker', '23:59', str(body),
               '--replaces', before.messages[-1].message_id]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'AGENT_COMMS_ROOT': str(comms.root),
                                 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    print(f'CLI exit {result.returncode}: {result.stderr.strip()}')
    assert result.returncode != 0
    assert f"which ['{toad}'] import" in result.stderr
    assert 'acp_ingress' in result.stderr
    assert comms.goals.todos.get('P1') == before
