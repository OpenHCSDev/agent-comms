"""Five bounded startups on real PTYs with controlled terminal protocol replies.

The terminal, not the application, supplies cell dimensions. The kernel initially
reports zero columns; a cell-size query receives a non-default declared fixture
answer before DA, and then the terminal publishes its ready window geometry.
No provider input, package installation, production patch or mock is involved.
"""
import fcntl,json,os,pathlib,pty,select,signal,struct,subprocess,sys,termios,time

root=pathlib.Path('/home/ts/wt/core-doctrine-segment/.scratch/p9')
runtime=pathlib.Path('/home/ts/.local/share/agent-comms/current').resolve()
stages=[]
for i,cell in enumerate(((7,15),(8,16),(9,17),(11,23),(13,25)),1):
    stage=root/f'run-{i}'
    stage.mkdir(exist_ok=False)
    (stage/'project').mkdir()
    (stage/'expected.json').write_text(json.dumps({'cell':cell}))
    env={k:v for k,v in os.environ.items() if not k.startswith(('AGENT_COMMS_','PI_','TEXTUAL_CELL_'))}
    env.update(AGENT_COMMS_ROOT=str(stage/'wire'), XDG_CONFIG_HOME=str(stage/'config'), XDG_DATA_HOME=str(stage/'data'), XDG_STATE_HOME=str(stage/'state'), XDG_CACHE_HOME=str(stage/'cache'), TERM='xterm-256color', COLORTERM='truecolor', PYTHONDONTWRITEBYTECODE='1')
    master,slave=pty.openpty()
    fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',40,0,0,0))
    def own_tty():
        os.setsid()
        fcntl.ioctl(0,termios.TIOCSCTTY,0)
    process=subprocess.Popen([str(runtime/'bin/python'),str(root/'child.py'),str(stage)],stdin=slave,stdout=slave,stderr=slave,env=env,cwd=stage/'project',preexec_fn=own_tty)
    os.close(slave)
    output=bytearray();pending=b'';queries=0;deadline=time.monotonic()+25
    try:
        while process.poll() is None:
            if time.monotonic()>deadline:raise TimeoutError(f'run {i} did not exit')
            ready,_,_=select.select([master],[],[],.1)
            if not ready:continue
            try:chunk=os.read(master,65536)
            except OSError:
                process.wait(timeout=5)
                break
            output.extend(chunk);pending+=chunk
            if b'\x1b[16t' in pending:
                queries+=1
                os.write(master,f'\x1b[6;{cell[1]};{cell[0]}t'.encode())
                fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',40,120,cell[0]*120,cell[1]*40))
                pending=pending.replace(b'\x1b[16t',b'')
            if b'\x1b[c' in pending:
                os.write(master,b'\x1b[?62;4c')
                pending=pending.replace(b'\x1b[c',b'')
            pending=pending[-64:]
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=5)
        os.close(master)
        (stage/'terminal.bin').write_bytes(output)
    assert process.returncode==0,(i,process.returncode,output[-5000:].decode(errors='replace'))
    receipt=json.loads((stage/'receipt.json').read_text())
    assert queries==1,(i,queries)
    assert b'ZeroDivisionError' not in output
    receipt.update(run=i,cell_query_count=queries,exit_code=process.returncode,terminal_response='controlled real-PTY cell-size/DA protocol',stage=str(stage))
    stages.append(receipt)
    print(json.dumps({'run':i,'cell':receipt['cell_size'],'first_frame':receipt['first_frame'],'zero_division':0}),flush=True)
result={'zero_division':0,'startups':len(stages),'passed':len(stages),'textual_image_sha':stages[0]['textual_image']['vcs_info']['commit_id'],'runtime':str(runtime),'application':'installed Toad, actual constructor, Linux terminal driver and first rendered frame','verification':'five fresh zero-column PTYs; controlled cell-size/DA terminal replies, not physical st/Xvfb acceptance','default_dimensions_used':False,'production_changes':'none; library fix already installed','runs':stages}
manifest=json.loads((runtime/'build.json').read_text())
result['toad_sha']=manifest['toad_commit']
result['core_sha']=manifest['core_commit']
result['source_matches_installed']=(pathlib.Path('/home/ts/code/projects/textual-image/textual_image/_terminal.py').read_bytes()==pathlib.Path(stages[0]['probe_file']).read_bytes())
path=pathlib.Path('/home/ts/.cache/agent-scratch/rts-results/p9.json');path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(result,indent=2)+'\n')
