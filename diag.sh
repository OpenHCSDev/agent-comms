BASE=39fdfe810e07655a3fd994244a1bf5192aaeea78
echo "== mypy"
python -m mypy src/agent_comms/plan_phases.py src/agent_comms/phase_evidence.py src/agent_comms/pinned_audit.py 2>&1 | grep -E "^src/agent_comms/(plan_phases|phase_evidence|pinned_audit)\.py" 
for kind in src tools; do
echo "== ratchet $kind"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root $kind --base $BASE --head HEAD 2>&1 | grep -E '^(introduced|grew|  )' | grep -vE '^\s+"[A-Za-z]+(:[^"]*)?": 0,?$' | head -150
done
