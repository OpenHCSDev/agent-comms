"""A contended scratch route reports the real flock owner without stopping it."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_comms.active_route import acquire_exclusive_route_lock
from agent_comms.errors import RelationViolationError


@pytest.mark.skipif(sys.platform != 'linux', reason='/proc/locks is Linux-specific')
def test_exclusive_route_lock_names_shared_holder(tmp_path: Path):
    path = tmp_path / 'route'
    path.mkdir()
    script = (
        'import fcntl, os, sys\n'
        'fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)\n'
        'fcntl.flock(fd, fcntl.LOCK_SH)\n'
        'sys.stdout.write("locked\\n"); sys.stdout.flush()\n'
        'sys.stdin.read(1)\n'
    )
    process = subprocess.Popen(
        [sys.executable, '-c', script, str(path)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline() == 'locked\n'
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with pytest.raises(RelationViolationError, match=rf'PID {process.pid}: .*python'):
                acquire_exclusive_route_lock(directory)
        finally:
            os.close(directory)
    finally:
        process.stdin.write('x')
        process.stdin.close()
        process.wait(timeout=5)
    assert process.returncode == 0
    directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        acquire_exclusive_route_lock(directory)
    finally:
        os.close(directory)
