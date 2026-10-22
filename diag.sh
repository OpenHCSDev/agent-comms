for b in afb5909c8 954534a8e 2a1fb1cd1; do
echo "== ratchet src $b..origin/main~0"; PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base $b --head origin/main 2>&1 | grep -E "^(introduced|grew)"
done
