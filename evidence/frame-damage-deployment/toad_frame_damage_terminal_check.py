import fcntl,os,pty,select,struct,subprocess,sys,termios
from pathlib import Path
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',40,139,0,0))
p=subprocess.Popen([sys.executable,'-u','evidence/frame-damage-deployment/toad_frame_damage_installed_pilot.py'],stdin=s,stdout=s,stderr=s,env=dict(os.environ,TERM='xterm-256color'),start_new_session=True)
os.close(s);data=[]
while True:
 if select.select([m],[],[],.1)[0]:
  try:data.append(os.read(m,65536))
  except OSError:break
 elif p.poll() is not None:break
p.wait();os.close(m);tape=b''.join(data).decode(errors='replace');Path('evidence/frame-damage-deployment/frame-terminal.txt').write_text(tape)
assert p.returncode==0,(p.returncode,tape[-2000:])
assert 'ACTUAL_TERMINAL_TOAD_RESIZE_MODE_RETURN_PASS' in tape
assert 'IndexError:' not in tape and 'Traceback' not in tape
print('PASS actual PTY Toad main150/current core6bd: narrow shrink/growth/resize, painted tail, mode switch/return, no uncaught exception')
