"""Run by test_ack_window.py in a fresh process: place a call, leave the lab at once, and let asyncio cancel whatever is left. A voice pipeline whose call ended before
it had started is not cancelled by the call's own cleanup, and then it does not die at the end of the loop either: the process hangs."""
import asyncio

from test_pipeline_fast import ASH, place, rig


async def main():
    async with rig([]) as r:
        await place(r, ASH, "+14155550179")


asyncio.run(main())
print("clean exit")
