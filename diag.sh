git fetch -q origin main
B=$(git merge-base HEAD origin/main)
echo "== ratchet src vs $B"; PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base $B --head HEAD 2>&1 | grep -E "^(introduced|grew)"
echo "== ratchet tools"; PYTHONPATH=src:tools python tools/debt_ratchet.py --root tools --base $B --head HEAD 2>&1 | grep -E "^(introduced|grew)"
echo "== pytest"; timeout 3000 python -m pytest -q -p no:cacheprovider -rfE --tb=line --no-header 2>&1 | grep -E "^(FAILED|ERROR)|passed|failed" | cut -c1-260
