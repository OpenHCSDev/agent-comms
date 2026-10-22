for base in 39fdfe810e07655a3fd994244a1bf5192aaeea78 3a2821822ec8579b1eb0fee89b69a1efb3e363a8; do
for kind in src tools; do
echo "== ratchet $kind base $base"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root $kind --base $base --head HEAD > /tmp/r.txt 2>&1; echo "exit $?"
grep -E '^(introduced|grew) |Traceback|CalledProcessError' /tmp/r.txt | head -20
done; done
