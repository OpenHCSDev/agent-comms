"""Run the unchanged pre-P91 Git source with the same selected dependencies."""
import subprocess

__package__ = "agent_comms"
exec(compile(subprocess.check_output([
    "git", "show", "10edd95d9:src/agent_comms/debt_ratchet.py"
]), "git:10edd95d9:src/agent_comms/debt_ratchet.py", "exec"), globals())
