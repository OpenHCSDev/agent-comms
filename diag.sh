echo "== import active_route"
python -c "import agent_comms.active_route" 2>&1 | tail -25
echo "== import plan_phases"
python -c "import agent_comms.plan_phases" 2>&1 | tail -25
echo "== stored_formats head"
python tools/stored_formats.py agent_comms.active_route 2>&1 | tail -30
echo "== ratchet src tail"
PYTHONPATH=src:tools python tools/debt_ratchet.py --root src --base 39fdfe810e07655a3fd994244a1bf5192aaeea78 --head HEAD 2>&1 | grep -vE '^\s+"[A-Za-z]+(:[^"]*)?": [0-9-]+,?$' | tail -60
