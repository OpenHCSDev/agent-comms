"""An actual importer and sidebar reader must finish, not lock each other out."""
import os
from pathlib import Path
import signal
import subprocess
import sys


def test_import_and_sidebar_observation_complete_on_a_real_private_bus(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    process = subprocess.Popen(
        [sys.executable, '-X', 'faulthandler', str(repository / 'tools/reproduce_thread_list.py'),
         str(tmp_path / 'wire'), '--seconds', '3', '--imports'],
        cwd=repository, env={**os.environ, 'PYTHONPATH': str(repository / 'src')},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=25)
        print(output)
        assert process.returncode == 0, output
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
