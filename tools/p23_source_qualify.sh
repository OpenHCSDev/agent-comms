#!/usr/bin/env bash
# Qualify declared cold roots, retain one source-stashed callback control, then run named consumers.
set -u
prefix=$1
label=$2
before_filter=$3
expected_failures=$4
cold_file=$5
tests_file=$6
errors_file=$7
callbacks_file=${8:-tests/test_goal_runtime_annotations.py}
python=${9:-/home/ts/code/projects/agent-comms/.venv/bin/python}
pythonpath=${10:-src}
mapfile -t roots < "$cold_file"
mapfile -t tests < "$tests_file"
mapfile -t errors < "$errors_file"
pytest=("$python" -m pytest -q -p no:xdist -o faulthandler_timeout=60)
callbacks=("${pytest[@]}" "$callbacks_file" -k "$before_filter")
run_consumers() {
    PYTHONPATH="$pythonpath" timeout -s INT 300 "${pytest[@]}" "${tests[@]}" -k 'not resolve_cold' > "$prefix-consumers.log" 2>&1
    result=$?
    printf '%s\n' "$result" > "$prefix-consumers.exit"
    return "$result"
}
if test "${11:-all}" = consumers; then
    run_consumers
    exit $?
fi

for first in "${roots[@]}"; do
    printf 'Cold entry %s\n' "$first"
    PYTHONPATH="$pythonpath" timeout -s INT 30 "$python" tests/goal_publication_probe.py "$first" || exit $?
done > "$prefix-cold.log" 2>&1
printf '0\n' > "$prefix-cold.exit"

PYTHONPATH="$pythonpath" timeout -s INT 60 "${callbacks[@]}" > "$prefix-hot-callbacks.log" 2>&1
hot=$?
printf '%s\n' "$hot" > "$prefix-hot-callbacks.exit"
if test "$hot" -ne 0; then exit "$hot"; fi

PYTHONPATH="$pythonpath" bash tools/p23_source_control.sh "$prefix-source-before" "$label" \
    timeout -s INT 90 "${callbacks[@]}"
before=$?
read -r restored < "$prefix-source-before-restore.exit" || exit $?
if test "$restored" -ne 0; then exit "$restored"; fi
if test "$before" -ne 1; then
    printf 'Expected collected callback failures, got exit %s\n' "$before"
    exit 1
fi
grep -q "$expected_failures failed" "$prefix-source-before.log" || exit 1
for error in "${errors[@]}"; do grep -q -F "$error" "$prefix-source-before.log" || exit 1; done

run_consumers
exit $?
