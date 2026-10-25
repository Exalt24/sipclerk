"""Shared by the tests that run the real speech models: one lab, one event loop in a thread, scenarios run on demand."""
from __future__ import annotations

import asyncio
import threading
import time

from sipclerk.call import CallResult
from sipclerk.harness import Played
from sipclerk.labrun import LabRig
from sipclerk.voice import models as vm
from voice_scripts.scenarios import BY_NAME


RECENT: list = []  # (name, (played, result)) touched by the running test, printed when it fails


class Suite:
    """Starts the lab once, runs a scenario the first time a test asks for it, and keeps the answer."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.cache: dict[str, tuple[Played, CallResult | None]] = {}
        self.rig: LabRig | None = None
        self.warm_info: dict = {}
        self.wall_s: dict[str, float] = {}

    def _call(self, coro, timeout: float = 600):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self) -> Suite:
        vm.quiet_pipecat_logs()

        async def go() -> None:
            models = vm.load()
            self.rig = LabRig(models)
            await self.rig.__aenter__()
            self.warm_info = await self.rig.warm()

        self._call(go(), 600)
        return self

    def get(self, name: str) -> tuple[Played, CallResult | None]:
        RECENT.append((name, self.cache[name])) if name in self.cache else None
        if name not in self.cache:
            t0 = time.monotonic()
            self.cache[name] = self._call(self.rig.run(BY_NAME[name]), 300)
            self.wall_s[name] = round(time.monotonic() - t0, 1)
            RECENT.append((name, self.cache[name]))
        return self.cache[name]

    def together(self, *names: str) -> list[tuple[Played, CallResult | None]]:
        """Run several scenarios at the same moment, as simultaneous callers."""
        async def go():
            return await asyncio.gather(*(self.rig.run(BY_NAME[n]) for n in names))

        out = self._call(go(), 300)
        for n, r in zip(names, out):
            self.cache[n] = r
            RECENT.append((n, r))
        return out

    def stop(self) -> None:
        self._call(self.rig.__aexit__(None, None, None), 60)
        self.loop.call_soon_threadsafe(self.loop.stop)

    def all_onsets(self) -> list[float]:
        return [x for played, _ in self.cache.values() for x in played.onsets_ms]
