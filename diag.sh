echo "== import"
python -c "import agent_comms.work_branches, agent_comms.active_route, agent_comms.plan_phases; print('imports ok')" 2>&1 | tail -15
echo "== mypy"
python -m mypy src/agent_comms/plan_phases.py src/agent_comms/phase_evidence.py 2>&1 | grep -E "^src/agent_comms/(plan_phases|phase_evidence)\.py"
echo "== pytest"
python -m pytest -q tests/test_phase_evidence.py 2>&1 | tail -3
for kind in src tools; do
echo "== ratchet $kind"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root $kind --base 39fdfe810e07655a3fd994244a1bf5192aaeea78 --head HEAD > /tmp/r.txt 2>&1; echo "ratchet exit $?"
grep -E '^(introduced|grew) |Error|Traceback' /tmp/r.txt | head -20
done
