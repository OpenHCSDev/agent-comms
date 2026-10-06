set pagination off
set confirm off
set debuginfod enabled off
set disable-randomization off
set startup-with-shell off
set follow-fork-mode parent
set detach-on-fork on
handle SIGTRAP stop print nopass
python
import gdb
import hashlib
import json
import os
from pathlib import Path
import time

# Only load this command file in the reviewed, original BoundedRun child.
# GDB launches the App itself; it does not attach to an independently found PID.
root = Path(os.environ['TOAD_NATIVE_WAIT_OUTPUT'])
root.mkdir(mode=0o700, exist_ok=False)
gdb.execute('starti')
# The existing controller acquires this inferior's separate process group
# through ProcessOwner.transfer before any application code is released.
print('GDB_INFERIOR ' + json.dumps({'pid': gdb.selected_inferior().pid,
                                  'parent_pid': os.getpid()}), flush=True)
if os.read(0, 2) != b'G\n':
    gdb.execute('kill')
    raise gdb.GdbError('Original controller did not acquire inferior custody')
gdb.execute('continue')
inferior = gdb.selected_inferior()
if inferior.pid:
    result = {'pid': inferior.pid, 'captured_ns': time.time_ns(), 'threads': [],
              'objects': [{'file': obj.filename, 'build_id': obj.build_id}
                          for obj in gdb.objfiles()], 'errors': []}
    try:
        result['stop_signal'] = int(gdb.parse_and_eval('$_siginfo.si_signo'))
        assert result['stop_signal'] == 5
        # capture() publishes through a background exporter. All-stop may
        # interrupt that writer after O_EXCL creates an empty JSON file.
        # Native observation must not depend on decoding it while stopped.
        libc = Path('/usr/lib/libc.so.6')
        actual_libc_sha = hashlib.sha256(libc.read_bytes()).hexdigest()
        result['libc_sha256'] = actual_libc_sha
        expected_libc_sha = os.environ['TOAD_NATIVE_WAIT_LIBC_SHA256']
        mapped_libc = any(Path(obj.filename).resolve() == libc.resolve()
                          for obj in gdb.objfiles())
        verified_libc = mapped_libc and actual_libc_sha == expected_libc_sha
        result['libc_matches_reviewed_mutex_abi'] = verified_libc
        for thread in inferior.threads():
            thread.switch()
            item = {'gdb_thread': thread.num, 'ptid': list(thread.ptid)}
            result['threads'].append(item)
            try:
                item['backtrace'] = gdb.execute('bt', to_string=True)
                item['registers'] = gdb.execute('info registers', to_string=True)
                frame = gdb.newest_frame()
                names = []
                while frame is not None:
                    names.append(frame.name())
                    frame = frame.older()
                item['frames'] = names
                for name in ('stat', 'syscall', 'wchan'):
                    path = Path('/proc') / str(inferior.pid) / 'task' / str(thread.ptid[1]) / name
                    try:
                        item[name] = path.read_text()
                    except OSError as error:
                        item[name + '_error'] = repr(error)
                syscall = int(gdb.parse_and_eval('$orig_rax'))
                item['orig_rax'] = syscall
                # x86-64 FUTEX_WAIT / FUTEX_WAIT_PRIVATE. Other waits are
                # retained as raw registers; they are not called mutex waits.
                if syscall == 202:
                    address = int(gdb.parse_and_eval('$rdi'))
                    operation = int(gdb.parse_and_eval('$rsi'))
                    item['futex'] = {'address': hex(address), 'operation': operation,
                                     'expected': int(gdb.parse_and_eval('$rdx'))}
                    is_mutex_frame = any(name in ('pthread_mutex_lock', '__pthread_mutex_lock',
                                                  '___pthread_mutex_lock') for name in names)
                    if verified_libc and operation in (0, 128) and is_mutex_frame:
                        # Reviewed host glibc's struct __pthread_mutex_s and
                        # actual pthread_mutex_lock disassembly: lock +0,
                        # count +4, owner +8, nusers +12, kind +16. The futex
                        # wait belongs to that lock, not a condition variable.
                        words = bytes(inferior.read_memory(address, 20))
                        item['mutex'] = {'address': hex(address), 'raw_hex': words.hex(),
                            'owner_tid': int.from_bytes(words[8:12], 'little', signed=True),
                            'kind': int.from_bytes(words[16:20], 'little', signed=True)}
            except Exception as error:
                item['capture_error'] = repr(error)
        result['mappings'] = (Path('/proc') / str(inferior.pid) / 'maps').read_text()
        tids = {item['ptid'][1] for item in result['threads']}
        for item in result['threads']:
            if 'mutex' in item:
                item['mutex']['owner_thread_captured'] = item['mutex']['owner_tid'] in tids
    except Exception as error:
        result['errors'].append(repr(error))
    finally:
        # Observation never replaces the original failure or its cleanup.
        # No inferior function calls, lock writes, timeout changes or detach.
        try:
            with (root / 'native-waits.json').open('x') as stream:
                json.dump(result, stream, indent=2)
        finally:
            gdb.execute('continue')
# Preserve the inferior's actual exit status in the original parent wait.
if gdb.selected_inferior().pid:
    raise gdb.GdbError('Unexpected later stop; original bounded owner must retire its group')
# Only after the original inferior exits can its exporter no longer be
# writing. This correlation has a different lifetime from the stopped native
# snapshot; a missing/incomplete export does not discard native evidence.
if 'result' in locals():
    correlation = {'native_capture_pid': result['pid'], 'errors': []}
    try:
        marker = Path(os.environ['L0A_EVIDENCE']) / 'start-completion-failure.json'
        raw = marker.read_bytes()
        python_capture = json.loads(raw)
        assert python_capture['pid'] == result['pid']
        payload = marker.with_suffix('.pickle')
        assert python_capture['payload_bytes'] == payload.stat().st_size
        correlation['python_capture'] = {'path': str(marker),
            'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        python_threads = {thread['native_id']: thread['name']
                          for thread in python_capture['python_threads']}
        correlation['threads'] = [{'ptid': thread['ptid'],
            'python_thread_name': python_threads.get(thread['ptid'][1])}
            for thread in result['threads']]
    except Exception as error:
        correlation['errors'].append(repr(error))
    with (root / 'python-capture-correlation.json').open('x') as stream:
        json.dump(correlation, stream, indent=2)
code = gdb.parse_and_eval('$_exitcode')
if code.type.code == gdb.TYPE_CODE_VOID:
    with (root / 'inferior-terminal.json').open('x') as stream:
        json.dump({'original_parent': 'GDB', 'exit_code': None,
                   'exit_signal': str(gdb.parse_and_eval('$_exitsignal'))}, stream)
    gdb.execute('quit 1')
else:
    with (root / 'inferior-terminal.json').open('x') as stream:
        json.dump({'original_parent': 'GDB', 'exit_code': int(code)}, stream)
    gdb.execute('quit ' + str(int(code)))
end
