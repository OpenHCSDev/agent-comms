import sys
from pathlib import Path
import agent_comms
assert Path(agent_comms.__file__).resolve().is_relative_to(Path(sys.prefix)), agent_comms.__file__
print('Installed Core:', agent_comms.__file__, flush=True)
sys.path.append('/home/ts/wt/core-doctrine-segment/.venv/lib/python3.14/site-packages')
if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
