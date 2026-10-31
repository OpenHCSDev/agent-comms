"""A process reading the board decodes every subject without importing the rules that wrote it."""

import subprocess
import sys


def test_every_subject_is_declared_with_the_family() -> None:
    probe = (
        "import sys\n"
        "from agent_comms.heartbeat_subjects import HeartbeatSubject\n"
        "assert \"agent_comms.commander.standing\" not in sys.modules\n"
        "names = {member.__name__ for member in HeartbeatSubject.members_with(HeartbeatSubject)}\n"
        "assert {\"RequestSubject\", \"PhaseSubject\"} <= names, names\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=60)
