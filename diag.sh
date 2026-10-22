for kind in src tools; do
  echo "=== ratchet $kind"
  PYTHONPATH=src:tools python tools/debt_ratchet.py --root $kind --base 39fdfe810 --head HEAD~1 2>&1 | grep -v '": 0,\?$' | tail -40
done
echo "=== mypy head"
MYPYPATH=src python -m mypy --explicit-package-bases --no-error-summary src/agent_comms/commander/order.py src/agent_comms/storage_rules.py src/agent_comms/shared_ledger.py | grep -E '^src/agent_comms/(commander/order|storage_rules)\.py'
