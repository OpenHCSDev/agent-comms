set -x
git worktree add -q /tmp/base 39fdfe810
for side in /tmp/base .; do
  echo "=== $side"
  (cd $side && MYPYPATH=src python -m mypy --explicit-package-bases --no-error-summary src/agent_comms/commander/order.py src/agent_comms/shared_ledger.py | grep -E '^src/agent_comms/(commander/order|shared_ledger)\.py')
done
