git fetch -q origin main
B=$(git merge-base HEAD origin/main)
echo "== mypy diff field_codec vs $B"; python -m mypy src/agent_comms/field_codec.py 2>&1 | grep "field_codec.py.*error" | sed 's/:[0-9]*:/:/' | sort > /tmp/h.txt
git worktree add -q /tmp/base $B; (cd /tmp/base && python -m mypy src/agent_comms/field_codec.py 2>&1 | grep "field_codec.py.*error" | sed 's/:[0-9]*:/:/' | sort > /tmp/b.txt)
diff /tmp/b.txt /tmp/h.txt
echo "== mypy head with lines"; python -m mypy src/agent_comms/field_codec.py 2>&1 | grep "field_codec.py.*error"
echo "== ratchet"; PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base $B --head HEAD 2>&1 | grep -E "^(introduced|grew)"
