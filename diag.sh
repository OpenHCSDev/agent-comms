echo "== pytest main"; timeout 3000 python -m pytest -q -p no:cacheprovider -rfE --tb=no --no-header 2>&1 | grep -E "^(FAILED|ERROR)|passed|failed" | sed -E 's/ - .*//' | cut -c1-200
