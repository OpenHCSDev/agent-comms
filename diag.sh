BASE=39fdfe810e07655a3fd994244a1bf5192aaeea78
echo "== mypy"
python -m mypy src/agent_comms/plan_phases.py src/agent_comms/phase_evidence.py src/agent_comms/pinned_audit.py 2>&1 | grep -E "^src/agent_comms/(plan_phases|phase_evidence|pinned_audit)\.py"
echo "== pytest"
python -m pytest -q -x tests/test_phase_evidence.py 2>&1 | tail -40
echo "== identity sites"
PYTHONPATH=src:tools python - <<'PY'
import sys
from pathlib import Path
sys.argv=["x"]
import debt_ratchet as d
repo = Path.cwd()
paths = d.production_paths(repo, "HEAD")
readings = {p: d.DeclaredSpellings.declarations(p, d.git(repo, "show", f"HEAD:{p}")) for p in sorted(paths)}
spell = d.DeclaredSpellings.read(readings)
for f in ("src/agent_comms/phase_evidence.py","src/agent_comms/plan_phases.py","src/agent_comms/pinned_audit.py"):
    for site in spell.sites(Path(f).read_bytes(), f):
        print(f, site)
PY
for kind in src tools; do
echo "== ratchet $kind"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root $kind --base $BASE --head HEAD 2>&1 | grep -E '^(introduced|grew) '
done
