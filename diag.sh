export MYPYPATH=src
F="src/agent_comms/launch_runtime.py src/agent_comms/child_process.py src/agent_comms/native_child.py src/agent_comms/pi_helper.py src/agent_comms/owner_launch.py src/agent_comms/selected_session.py src/agent_comms/native_pi.py src/agent_comms/installer/upgrade.py src/agent_comms/config_options.py src/agent_comms/owner_restart.py"
count() { python -m mypy --explicit-package-bases --no-error-summary $F 2>&1 | grep -oE "^src/[^:]+\.py:[0-9]+: error" | cut -d: -f1 | sort | uniq -c; }
python -m mypy --explicit-package-bases --no-error-summary $F 2>&1 | grep -E "^src/agent_comms/launch_runtime\.py" > /tmp/lr.txt
count > /tmp/head.txt
git checkout -q origin/main -- src
count > /tmp/base.txt
echo "== launch_runtime at head"; cat /tmp/lr.txt
echo "== per-file base vs head"; join -1 2 -2 2 -a1 -a2 -e0 -o 0,1.1,2.1 <(sort -k2 /tmp/base.txt) <(sort -k2 /tmp/head.txt)
