git fetch -q origin main
B=$(git merge-base HEAD origin/main)
echo "== mypy head"; python -m mypy src/agent_comms/field_codec.py 2>&1 | grep "field_codec.py" > head.txt; cat head.txt
git worktree add -q /tmp/base $B
(cd /tmp/base && python -m mypy src/agent_comms/field_codec.py 2>&1 | grep "field_codec.py" > /tmp/base.txt)
echo "== base count $(wc -l < /tmp/base.txt) head count $(wc -l < head.txt)"
echo "== ratchet"; PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base origin/main --head HEAD 2>&1 | grep -E "^(introduced|grew)"
