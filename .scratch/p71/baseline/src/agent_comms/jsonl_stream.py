"""The original cancellation-safe JSONL transport resource, without a codec."""
import asyncio

class JsonlStreamReader:
    def __init__(self, reader: asyncio.StreamReader):
        self.reader = reader
        self.chunks: list[bytes] = []

    async def readline(self, *, max_bytes: int | None = None) -> bytes:
        size = sum(map(len, self.chunks))
        while True:
            try:
                line = await self.reader.readuntil(b"\n")
            except asyncio.LimitOverrunError as error:
                if max_bytes is not None and size + error.consumed > max_bytes:
                    self.chunks.clear()
                    raise ValueError("Native RPC record exceeds transport limit") from error
                self.chunks.append(await self.reader.readexactly(error.consumed))
                size += error.consumed
                continue
            except asyncio.IncompleteReadError as error:
                line = error.partial
            if max_bytes is not None and size + len(line) > max_bytes:
                self.chunks.clear()
                raise ValueError("Native RPC record exceeds transport limit")
            self.chunks.append(line)
            record = b"".join(self.chunks)
            self.chunks.clear()
            return record

