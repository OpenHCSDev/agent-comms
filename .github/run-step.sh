#!/usr/bin/env bash
# Runs one workflow step. A failing step's last lines become check annotations, which the
# checks API serves to readers that cannot fetch job logs (the connector reviewer).
set -o pipefail
log=$(mktemp)
bash -eo pipefail "$1" 2>&1 | tee "$log"
status=${PIPESTATUS[0]}
if [ "$status" -ne 0 ]; then
  tail -n 10 "$log" | sed -e 's/%/%25/g' -e "s/^/::error title=${GITHUB_JOB} failed::/"
fi
exit "$status"
