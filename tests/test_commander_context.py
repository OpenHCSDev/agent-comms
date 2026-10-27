"""A command owns its selected bus; importing its declarations opens nothing."""
import os
import subprocess
import sys




def test_importing_order_with_empty_home_reads_no_config_and_opens_no_store(tmp_path):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('AGENT_COMMS_', 'PI_COMMS_', 'PI_NATIVE_'))}
    environment['HOME'] = str(tmp_path)
    result = subprocess.run([sys.executable, '-B', '-c', '''
import os
import sys
from pathlib import Path

def no_runtime_files(event, arguments):
    if event == 'sqlite3.connect':
        raise AssertionError('import opened a database')
    if event in {'open', 'os.listdir', 'os.scandir'}:
        path = arguments[0]
        if isinstance(path, (str, bytes)) and Path(os.fsdecode(path)).is_relative_to(Path.home()):
            raise AssertionError('import opened a runtime file or read configuration')

sys.addaudithook(no_runtime_files)
import agent_comms.commander.order
'''], env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not tuple(tmp_path.iterdir())


