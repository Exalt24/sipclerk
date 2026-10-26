"""Run by test_ack_window.py in a fresh process: place a call, leave the lab at once, and let asyncio cancel whatever is left. When the line stops with a call just
started, the pipeline of that call must be ended by `stop`: one left behind does not die at the end of the loop either, and the process hangs."""
import asyncio

from test_pipeline_fast import ASH, place, rig


async def main():
    async with rig([]) as r:
        await place(r, ASH, "+14155550179")


asyncio.run(main())
print("clean exit")
