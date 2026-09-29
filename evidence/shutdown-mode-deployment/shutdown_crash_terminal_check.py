import fcntl,os,pty,select,struct,subprocess,sys,termios
from pathlib import Path
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',40,139,0,0))
p=subprocess.Popen([sys.executable,'-u','evidence/shutdown-mode-deployment/shutdown_crash_terminal_pilot.py'],stdin=s,stdout=s,stderr=s,env=dict(os.environ,TERM='xterm-256color'),start_new_session=True)
os.close(s);data=[]
while True:
 if select.select([m],[],[],.1)[0]:
  try:data.append(os.read(m,65536))
  except OSError:break
 elif p.poll() is not None:break
p.wait();os.close(m);tape=b''.join(data).decode(errors='replace');Path('evidence/shutdown-mode-deployment/shutdown-terminal.txt').write_text(tape)
assert p.returncode==0,(p.returncode,tape[-2200:])
assert 'PASS original IndexError retained' in tape
assert 'dictionary changed size' not in tape
print('PASS actual PTY resize/mode-return->controlled UI IndexError->unmount remove_mode: original failure remains, all resources destroyed, no shutdown RuntimeError')
