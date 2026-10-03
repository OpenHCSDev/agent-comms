import asyncio,json,time
from agent_comms.runtime import socket_path
from agent_comms.runtime_requests import SubscribeRuntimeRequest

def ready(root, owner):
    async def attach():
        async with asyncio.timeout(20):
            while not socket_path(root,owner.pid).exists():
                assert owner.process_alive,'Original owner exited before attachment'
                await asyncio.sleep(.02)
            reader,writer=await asyncio.open_unix_connection(socket_path(root,owner.pid),limit=8*1024*1024)
            try:
                writer.write((json.dumps(SubscribeRuntimeRequest(thread=owner.name).to_wire())+'\n').encode())
                await writer.drain()
                while line:=await reader.readline():
                    row=json.loads(line); assert 'error' not in row,row
                    if 'ready' in row: return row['ready']
                raise AssertionError('Owner closed before ready')
            finally:
                writer.close(); await writer.wait_closed()
    return asyncio.run(attach())
