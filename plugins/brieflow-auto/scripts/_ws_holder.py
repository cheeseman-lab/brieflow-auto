import asyncio
import sys
import websockets


async def _hold(port, sid):
    uri = f"ws://localhost:{port}/ws?session_id={sid}"
    async with websockets.connect(
        uri, max_size=None, ping_interval=20
    ) as ws:
        try:
            while True:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=60)
                except asyncio.TimeoutError:
                    continue
        except websockets.ConnectionClosed:
            return


asyncio.run(_hold(int(sys.argv[1]), sys.argv[2]))
