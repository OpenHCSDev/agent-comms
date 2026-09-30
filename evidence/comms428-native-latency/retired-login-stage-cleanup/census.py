from pathlib import Path
import json, os, time

OUT = Path(__file__).parent
CANDIDATES = [Path('/home/ts/.local/share/agent-comms') / n for n in (
    'runtime-provider-login-candidate-20260930',
)]
NEEDLES = [str(p).encode() for p in CANDIDATES]

def matches(data):
    return [str(p) for p, n in zip(CANDIDATES, NEEDLES) if n in data]

def proc_census():
    refs, errors = [], []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            same_user = proc.stat().st_uid == Path('/home/ts').stat().st_uid
            if not same_user:
                continue
            status = (proc / 'status').read_text()
            if any(line.startswith('State:') and 'Z (zombie)' in line for line in status.splitlines()):
                continue
            for entry in ('exe', 'cwd', 'root'):
                try:
                    hits = matches(os.readlink(proc / entry).encode())
                    if hits: refs.append(dict(pid=int(proc.name), kind=entry, paths=hits))
                except FileNotFoundError: pass
            for entry in ('cmdline', 'environ', 'maps'):
                try:
                    hits = matches((proc / entry).read_bytes())
                    if hits: refs.append(dict(pid=int(proc.name), kind=entry, paths=hits))
                except FileNotFoundError: pass
            for fd in (proc / 'fd').iterdir():
                try:
                    hits = matches(os.readlink(fd).encode())
                    if hits: refs.append(dict(pid=int(proc.name), kind='fd', fd=fd.name, paths=hits))
                except FileNotFoundError: pass
        except FileNotFoundError: pass
        except PermissionError as exc:
            errors.append(dict(pid=int(proc.name), error=type(exc).__name__))
    return dict(references=refs, inaccessible_same_user_processes=errors)

def config_census():
    refs, errors, scanned = [], [], 0
    roots = [Path(p) for p in ('/home/ts/wt', '/home/ts/.cache/agent-scratch',
        '/home/ts/.agent-comms', '/home/ts/.local/state/agent-comms',
        '/home/ts/.local/share/agent-comms', '/var/tmp')]
    metadata_names = {'registry.json', 'active-route.json', 'runtime-selection.json',
        'runtime.json', 'runtime-config.json', 'route.json', 'selection.json', 'pyvenv.cfg'}
    skipped_dirs = {'.git', 'node_modules', '__pycache__', 'logs', 'sessions', 'history'}
    for root in roots:
        for current, dirs, files in os.walk(root):
            here = Path(current)
            dirs[:] = [d for d in dirs if d not in skipped_dirs and here / d not in CANDIDATES]
            for name in files:
                f = here / name
                selected = name in metadata_names or name.endswith('.pth')
                if name == 'direct_url.json':
                    try:
                        value = json.loads(f.read_bytes())
                        selected = value.get('dir_info', {}).get('editable', False)
                    except (OSError, ValueError): pass
                if not selected: continue
                try:
                    scanned += 1
                    hits = matches(f.read_bytes())
                    if hits: refs.append(dict(file=str(f), paths=hits))
                except OSError as exc: errors.append(dict(file=str(f), error=type(exc).__name__))
    launchers = []
    for root in (Path('/home/ts/bin'), Path('/home/ts/.local/bin')):
        for f in root.iterdir():
            try:
                if f.is_symlink():
                    target = os.readlink(f)
                    if 'agent-comms/runtime-' in target: launchers.append(dict(path=str(f), target=target))
                    hits = matches(target.encode())
                elif f.is_file() and f.stat().st_size < 1000000:
                    hits = matches(f.read_bytes())
                else: continue
                if hits: refs.append(dict(file=str(f), paths=hits))
            except OSError as exc: errors.append(dict(file=str(f), error=type(exc).__name__))
    route = json.loads(Path('/home/ts/.local/state/agent-comms/active-route.json').read_bytes())
    return dict(references=refs, errors=errors, files_scanned=scanned,
                launchers=launchers, active_route=route)

result = dict(timestamp=time.time(), owner='Schrodinger', candidates=[str(p) for p in CANDIDATES],
    processes=proc_census(), configuration=config_census())
target = OUT / 'reference-census.json'
target.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
