set -x
export MYPYPATH=src
F="src/agent_comms/launch_runtime.py src/agent_comms/child_process.py src/agent_comms/native_child.py src/agent_comms/pi_helper.py"
python -m mypy --explicit-package-bases --no-error-summary $F 2>&1 | grep -E "^src/agent_comms/(launch_runtime|native_child|child_process|pi_helper)\.py" > /tmp/head.txt
git checkout -q origin/main -- src
python -m mypy --explicit-package-bases --no-error-summary $F 2>&1 | grep -E "^src/agent_comms/(launch_runtime|native_child|child_process|pi_helper)\.py" > /tmp/base.txt
echo "== HEAD launch_runtime"; grep launch_runtime /tmp/head.txt
echo "== BASE launch_runtime"; grep launch_runtime /tmp/base.txt
echo "== HEAD others"; grep -v launch_runtime /tmp/head.txt
echo "== BASE others"; grep -v launch_runtime /tmp/base.txt
