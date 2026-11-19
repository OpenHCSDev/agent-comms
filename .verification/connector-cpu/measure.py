from pathlib import Path
import json,os,sys,time
pid=int(sys.argv[1]); result=Path(sys.argv[2]); stat=Path(f"/proc/{pid}/stat")
def ticks():
    f=stat.read_text().rsplit(")",1)[1].split();return int(f[11])+int(f[12])
started=time.monotonic(); previous=ticks(); rows=[]
for _ in range(6):
    time.sleep(10)
    now=time.monotonic(); current=ticks()
    rows.append({"elapsed":now-started,"cpu_seconds":(current-previous)/os.sysconf("SC_CLK_TCK")})
    previous=current
    result.write_text(json.dumps(rows,indent=2))
print(json.dumps({"pid":pid,"seconds":rows[-1]["elapsed"],"cpu_seconds":sum(r["cpu_seconds"] for r in rows),"cpu_percent":100*sum(r["cpu_seconds"] for r in rows)/rows[-1]["elapsed"]}))
