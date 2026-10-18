"""One-shot original-format controller for the authorized retained-bus upgrade.

Run with the original Python. Only original Core may decode and retire original
owners. The existing maintained cutover owns fencing, process checks and the
all-stopped wire resource; a target subprocess inherits that exact descriptor.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

ORIGINAL = Path('/home/ts/wt/comms-update-original')
CANDIDATE = Path('/home/ts/wt/comms-update-latest')
MAIN = Path('/home/ts/code/projects/agent-comms')
EVIDENCE = CANDIDATE / 'upgrade-evidence'
sys.path.insert(0, str(ORIGINAL / 'src'))

from agent_comms.active_route import active_route_path, route_lock_holders
from agent_comms.child_process import ProcessIdentity
from agent_comms.comms import wire
from agent_comms.field_codec import FieldCodec
from agent_comms.goal_actions import SetGoalAction
from agent_comms.launch_runtime import LaunchRuntime, RestartEnvironment
from agent_comms.maintenance_barrier import MaintenanceOperator, DrainingPhase, PausedPhase, InstallingPhase, ReadyPhase
from agent_comms.owner_cutover import MaintainedOwnerCutover
from agent_comms.store_files import _store_lock
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


def run(*args, **kwargs):
    print('RUN', *(str(a) for a in args), flush=True)
    return subprocess.run(args, check=True, **kwargs)


def close_frontends():
    descriptor = os.open(active_route_path().parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        holders = route_lock_holders(descriptor)
        for pid in sorted({h.pid for h in holders if h.command == 'toad'}):
            process = ProcessIdentity.capture(pid)
            event = os.pidfd_open(pid)
            try:
                if process.alive():
                    os.kill(pid, signal.SIGINT)
                poll = select.poll(); poll.register(event, select.POLLIN)
                if not poll.poll(15000):
                    raise RuntimeError(f'Toad {pid} did not shut down normally; not killing it')
            finally:
                os.close(event)
        # Fail closed on any remaining borrower. Never interrupt an unknown process.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


@dataclass
class Upgrade(MaintainedOwnerCutover):
    maintenance_hold: object
    originals: dict
    target_commit: str

    def require_selection(self, snapshot, owners):
        actual = {t.name: t.incarnation for t in owners}
        if actual != self.originals:
            raise RuntimeError('Original fleet changed before the maintained stop')

    def complete(self, stopped):
        route_fd = close_frontends()
        try:
            backup = EVIDENCE / 'original-root'
            assert not backup.exists(), 'Never overwrite an earlier migration preimage'
            shutil.copytree(stopped.lifecycle.root, backup,
                            ignore=shutil.ignore_patterns('runtime', '*.sock'))
            backup.chmod(0o700)
            (EVIDENCE / 'original-active-route.json').write_bytes(active_route_path().read_bytes())
            (EVIDENCE / 'original-native-manifest').write_bytes((MAIN / 'stack/pi-native.sha256').read_bytes())
            candidate = Path(subprocess.check_output([str(CANDIDATE/'stack/.venv/bin/python'),'-c',
                'from agent_comms.native_package import pinned_native_package; print(pinned_native_package())'],text=True).strip())
            source_backup=EVIDENCE/'native-source-preimages';source_backup.mkdir(mode=0o700)
            declarations=json.loads((candidate/'dist/agent-comms-imports.json').read_text())
            originals=[]
            for entry in declarations['extensionEntries']:
                for source in entry.get('sources',[]):
                    path=Path(source['path']);record={'path':str(path),'present':path.exists() or path.is_symlink()}
                    if path.is_symlink():record['symlink']=os.readlink(path)
                    if path.is_file():
                        saved=source_backup/str(len(originals));saved.write_bytes(path.read_bytes());saved.chmod(0o600)
                        record['preimage']=saved.name
                    originals.append(record)
            (source_backup/'index.json').write_text(json.dumps(originals,indent=2))
            # Incompatible installation is explicit: actual stopped/route custody
            # is already held, so advance the existing phase owner under registry.
            with _store_lock(stopped.lifecycle.registry.store.path):
                self.maintenance_hold=stopped.lifecycle.maintenance._advance_unlocked(self.maintenance_hold,PausedPhase)
                self.maintenance_hold=stopped.lifecycle.maintenance._advance_unlocked(self.maintenance_hold,InstallingPhase)
            # No commit, reset or clean in live main. The one reviewed old local
            # manifest difference is preserved above, then superseded by this build.
            dirty = subprocess.check_output(['git','-C',str(MAIN),'status','--porcelain'],text=True)
            if any(line[3:] != 'stack/pi-native.sha256' for line in dirty.splitlines()):
                raise RuntimeError('Unexpected local main changes; preserved, not overwritten')
            old_stashes = subprocess.check_output(['git','-C',str(MAIN),'stash','list','--format=%H'],text=True).splitlines()
            run('git','-C',str(MAIN),'merge','--ff-only','--autostash',self.target_commit)
            run('git','-C',str(MAIN),'restore','--source=HEAD','--staged','--worktree','--','stack/pi-native.sha256')
            stashes = subprocess.check_output(['git','-C',str(MAIN),'stash','list','--format=%H'],text=True).splitlines()
            if stashes != old_stashes:
                if stashes[1:] != old_stashes:
                    raise RuntimeError('Unexpected stash change; preserve it for inspection')
                paths = subprocess.check_output(['git','-C',str(MAIN),'diff',stashes[0]+'^1',stashes[0],'--name-only'],text=True).splitlines()
                saved = subprocess.check_output(['git','-C',str(MAIN),'show',stashes[0]+':stack/pi-native.sha256'])
                if paths != ['stack/pi-native.sha256'] or saved != (EVIDENCE/'original-native-manifest').read_bytes():
                    raise RuntimeError('Autostash has unreviewed changes; preserve it')
                run('git','-C',str(MAIN),'stash','drop','stash@{0}')
            run('uv','sync','--project',str(MAIN/'stack'),'--locked')
            deployment = MAIN/'stack'/candidate.relative_to(CANDIDATE/'stack')
            if deployment.exists():
                raise RuntimeError('Refusing to overwrite a native deployment')
            deployment.parent.mkdir(parents=True,exist_ok=True)
            shutil.copytree(candidate,deployment,symlinks=True)
            run(str(MAIN/'stack/.venv/bin/python'),str(MAIN/'src/agent_comms/native_package.py'),
                '--install-sources',str(deployment),str(MAIN))
            payload = {'handoff': FieldCodec.encode(stopped.handoff),
                       'maintenance': FieldCodec.encode(self.maintenance_hold)}
            environment = dict(os.environ)
            environment.pop('PYTHONPATH',None)
            environment['VIRTUAL_ENV']=str(MAIN/'stack/.venv')
            run(str(MAIN/'stack/.venv/bin/python'),str(EVIDENCE/'operation/target.py'),
                str(stopped.wire_descriptor),str(route_fd),str(EVIDENCE/'receipt.json'),
                input=json.dumps(payload),text=True,env=environment,
                pass_fds=(stopped.wire_descriptor,route_fd),timeout=180)
            return ()
        finally:
            os.close(route_fd)

    def recover(self, stopped):
        # Recovery is allowed only before any source/runtime/schema publication.
        # Prove the original files and schema, not a guessed exception category.
        head=subprocess.check_output(['git','-C',str(MAIN),'rev-parse','HEAD'],text=True).strip()
        dirty=subprocess.check_output(['git','-C',str(MAIN),'status','--porcelain'],text=True)
        rows=json.loads((stopped.lifecycle.root/'registry.json').read_text())['threads']
        if (not head.startswith('04e06f6f')
                or any(line[3:]!='stack/pi-native.sha256' for line in dirty.splitlines())
                or (MAIN/'stack/pi-native.sha256').read_bytes()!=(ORIGINAL/'stack/pi-native.sha256').read_bytes()
                or any('storage_roots' in row or 'previous_worktrees' not in row for row in rows.values())):
            raise RuntimeError('Original source/schema no longer certified unchanged; leave stopped')
        if self.maintenance_hold.phase is DrainingPhase:
            stopped.lifecycle.maintenance.clear_stopped(self.maintenance_hold,stopped.wire_descriptor)
        else:
            with _store_lock(stopped.lifecycle.registry.store.path):
                stopped.lifecycle.maintenance._advance_unlocked(self.maintenance_hold,ReadyPhase)
        return stopped.handoff.restore(stopped.lifecycle)

    def failed(self, failure):
        (EVIDENCE/'failure.txt').write_text(repr(failure.__cause__))
        # The existing cutover owner invokes recover once and leaves the exact
        # stopped phase held if unchanged-original proof fails. No input retries.
        self.restore_unchanged(failure)


def main():
    comms = wire()
    selected = [t for t in comms.registry.snapshot().threads.values()
                if t.process_alive and t.role.executable]
    originals = {t.name:t.incarnation for t in selected}
    expected = subprocess.check_output(['git','-C',str(MAIN),'rev-parse','HEAD'],text=True).strip()
    assert expected.startswith('04e06f6f'), 'Original live source moved before capture'
    target_commit = (EVIDENCE/'reviewed-main-commit').read_text().strip()
    changed = threading.Event()
    handler = FileSystemEventHandler()
    handler.on_any_event = lambda event: changed.set() if 'registry.json' in (event.src_path + str(getattr(event,'dest_path',''))) else None
    observer = Observer(); observer.schedule(handler,str(comms.root));observer.start()
    hold = comms.owners.maintenance.begin(MaintenanceOperator('codex-agent-comms upgrade #1355'))
    try:
        current = comms.registry.require('codex-agent-comms')
        if current.goal is not None:
            comms.owners.maintenance.clear(hold)
            raise RuntimeError('Existing goal must not be replaced by the upgrade')
        # Continue the same authorized task after this owner is idle-restarted.
        # The standard goal scheduler, not another queue or replayed input, owns it.
        goal = comms.goals.update_goal('codex-agent-comms', SetGoalAction(text=(
            'Finish acceptance of the user-authorized latest-main upgrade #1355. '
            'The stopped-owner controller owns installation; DO NOT rerun it or replay any input. '
            'Read /home/ts/wt/comms-update-latest/upgrade-evidence/receipt.json and operation.log. '
            'The controller also runs operation/verify.py for official ACP attachment. '
            'Verify the installed toad-comms saved-history and attachment UI in plain st on an '
            'isolated display, preserve original sessions/auth/wire/UNKNOWN inputs, then report '
            'the exact delivered result to the user and complete this goal. If installation '
            'is incomplete, inspect its retained outcome; never retry uncertain writes.')))
        print('Post-upgrade verification goal:', goal.id, flush=True)
        deadline = time.monotonic()+900
        while True:
            changed.clear()
            snapshot = comms.registry.snapshot()
            if all(snapshot.require(name).active_turn is None for name in originals):
                break
            remaining=deadline-time.monotonic()
            if remaining<=0 or not changed.wait(remaining):
                # No owner was fenced and no source/store migration began.
                comms.owners.maintenance.clear(hold)
                raise RuntimeError('Active turn did not finish; safely aborted before stop')
    finally:
        observer.stop();observer.join()
    print('All original turns finished; acquiring stopped custody',flush=True)
    operation = Upgrade(hold,originals,target_commit)
    environment=RestartEnvironment.inherit(os.environ).without_pythonpath()
    comms.owners.restart_owners(tuple(originals),runtime=environment,
        source_interpreter=str(MAIN/'stack/.venv/bin/python'),cutover=operation)
    # Both inherited locks are now retired. Verify/notify through installed new
    # Core; do not interpret or rewrite migrated data through this old decoder.
    environment=dict(os.environ);environment.pop('PYTHONPATH',None)
    run(str(MAIN/'stack/.venv/bin/python'),str(EVIDENCE/'operation/verify.py'),env=environment,timeout=90)


if __name__=='__main__':
    main()
