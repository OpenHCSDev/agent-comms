#!/usr/bin/env bash
# Retain a source-only stash, run an unchanged control command, always restore that source.
set -u
prefix=$1
label=$2
shift 2
git stash push --include-untracked -m "$label" -- src > "$prefix-stash.log" 2>&1 || exit $?
stash=$(git rev-parse refs/stash) || exit $?
printf '%s\n' "$stash" > "$prefix.stash"
restore() {
    git stash apply "$stash" > "$prefix-restore.log" 2>&1
    restored=$?
    printf '%s\n' "$restored" > "$prefix-restore.exit"
    return "$restored"
}
trap restore EXIT
"$@" > "$prefix.log" 2>&1
result=$?
printf '%s\n' "$result" > "$prefix.exit"
restore || exit $?
trap - EXIT
exit "$result"
