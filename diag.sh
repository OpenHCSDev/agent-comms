BASE=$(git rev-parse HEAD^1)
echo "== import"
python -c "import agent_comms.active_route; print('ok')" 2>&1 | tail -20
for rev in 3a2821822ec8579b1eb0fee89b69a1efb3e363a8 HEAD; do
echo "== stored_formats at $rev"
rm -rf /tmp/t && mkdir /tmp/t && git archive $rev src | tar -x -C /tmp/t
python -c "import importlib.util, os, sys; sys.path[:0] = sys.argv[2].split(os.pathsep); spec = importlib.util.spec_from_file_location('stored_formats', sys.argv[1]); module = sys.modules[spec.name] = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); module.main(sys.argv[3:])" tools/stored_formats.py /tmp/t/src agent_comms.active_route 2>&1 | tail -25 | cut -c1-400
done
echo "== ratchet src vs main"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base 3a2821822ec8579b1eb0fee89b69a1efb3e363a8 --head HEAD > /tmp/r.txt 2>&1; echo "exit $?"
grep -E '^(introduced|grew) ' /tmp/r.txt; tail -5 /tmp/r.txt | cut -c1-300
