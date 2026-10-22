git fetch -q origin main
B=$(git merge-base HEAD origin/main)
files=$(git diff --name-only --diff-filter=AM $B HEAD -- 'src/*.py' 'tools/*.py')
echo "== ruff fix and format diff"
ruff check --fix --quiet $files >/dev/null 2>&1; ruff format --quiet $files >/dev/null 2>&1
git diff > /tmp/ruff.patch; echo "patch lines $(wc -l < /tmp/ruff.patch)"; cat /tmp/ruff.patch
git checkout -q -- .
echo "== ruff remaining"; ruff check $files 2>&1 | grep -E "^[a-z].*:[0-9]+:[0-9]+:" | head -80
echo "== mypy new errors"
python -m mypy $files 2>/dev/null | grep ": error:" | sed 's/:[0-9]*: error/: error/' | sort > /tmp/head.txt
git worktree add -q /tmp/base $B
(cd /tmp/base && python -m mypy $(cd /tmp/base && for f in $files; do [ -f "$f" ] && echo $f; done) 2>/dev/null | grep ": error:" | sed 's/:[0-9]*: error/: error/' | sort > /tmp/base.txt)
comm -13 /tmp/base.txt /tmp/head.txt > /tmp/new.txt
python -m mypy $files 2>/dev/null | grep ": error:" | grep -F -f <(sed 's/^\([^:]*\): error: \(.*\)$/\2/' /tmp/new.txt | sort -u | head -200) | cut -c1-220
