"""A whole lab on loopback: the line, two desk phones, a caller softphone, stores and a receiver stub.

Used by the voice regression suite, the replay script and the demo. Everything is real except the far
ends of the two webhooks, which are a local stub unless a real receiver URL is given.
"""
from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sipclerk import delivery
from sipclerk.call import CallResult, Faults
from sipclerk.harness import Played, make_registry, play
from sipclerk.server import ClerkServer
from sipclerk.sip.phone import StaffEndpoint
from sipclerk.sip.stack import SipStack
from sipclerk.store import TenantStore
from sipclerk.voice.llm import Llm
from sipclerk.voice.models import Models
from voice_scripts.scenarios import ASHGROVE, Scenario

# Tuesday 10:30 in Los Angeles and 13:30 in Miami, and a Sunday night
IN_HOURS = datetime(2026, 10, 27, 17, 30)
AFTER_HOURS = datetime(2026, 11, 1, 3, 0)
BRIDGE_SECRET = "lab-bridge-secret"
CONSOLE_SECRET = "demo-only-webhook-secret-change-me"  # the console's documented demo secret


@dataclass
class StubReceiver:
    """Stands in for both webhook receivers: checks nothing itself, replays a scripted status list, remembers what arrived."""
    statuses: list[int] = field(default_factory=list)
    received: list[tuple[str, dict, dict]] = field(default_factory=list)

    async def __call__(self, url: str, body: bytes, headers: dict[str, str]) -> tuple[int, str]:
        import json

        self.received.append((url, headers, json.loads(body)))
        return (self.statuses.pop(0) if self.statuses else 200), "{}"


class LabRig:
    def __init__(self, models: Models | None, *, timeout_scale: float = 0.5, data_dir: Path | None = None,
                 sender=None, llm_factory=Llm, master_key: bytes | None = None, host: str = "127.0.0.1", port: int = 0, rtp_host: str = "127.0.0.1", bridge_url: str = "http://127.0.0.1:9/hook", console_url: str = "http://127.0.0.1:9/ingest"):
        self.models = models
        self.master_key = master_key
        self.host, self.port, self.rtp_host = host, port, rtp_host
        self.timeout_scale = timeout_scale
        self.data_dir = Path(data_dir or tempfile.mkdtemp(prefix="sipclerk-data-"))
        self.stub = StubReceiver()
        self.sender = sender or self.stub
        self.llm_factory = llm_factory
        self.bridge_url, self.console_url = bridge_url, console_url
        self.now = IN_HOURS
        self.faults = Faults()
        self.staff: dict[str, StaffEndpoint] = {}
        self.staff_stacks: list[SipStack] = []
        self.results: list[CallResult] = []

    async def __aenter__(self) -> LabRig:
        ports = {}
        for tenant in ("ashgrovepell", "tidewaterquint"):
            stack = SipStack("127.0.0.1", 0)
            await stack.start()
            self.staff[tenant] = StaffEndpoint(stack)
            self.staff_stacks.append(stack)
            ports[tenant] = stack.port
        self.registry = make_registry(ports, self.timeout_scale, ring_timeout_s=6)
        from sipclerk.server import master_key_from_env

        self.store = TenantStore(self.data_dir, self.registry, self.master_key or master_key_from_env(), clock=lambda: self.now)
        self.outbox = delivery.Outbox(self.data_dir / "outbox.jsonl", {
            "bridge": delivery.Target("bridge", self.bridge_url, BRIDGE_SECRET),
            "console": delivery.Target("console", self.console_url, CONSOLE_SECRET)}, sender=self.sender, keys=self.store.key_for)
        self.server = ClerkServer(registry=self.registry, models=self.models, store=self.store, outbox=self.outbox, host=self.host, port=self.port,
                                  rtp_host=self.rtp_host, now_fn=lambda: self.now, llm_factory=self.llm_factory, faults=self.faults,
                                  on_finished=self.results.append)
        await self.server.start()
        self.caller_stack = SipStack("127.0.0.1", 0)
        await self.caller_stack.start()
        return self

    async def warm(self) -> dict:
        """Load the first transcription and render every fixed line, so the first call does not pay for a cold start."""
        from sipclerk.voice import models as vm

        return await vm.warm(self.models, vm.fixed_lines(self.registry))

    async def __aexit__(self, *exc) -> None:
        await self.caller_stack.stop()
        await self.server.stop()
        for s in self.staff_stacks:
            await s.stop()

    async def run(self, sc: Scenario, wait_for_record: float = 20.0) -> tuple[Played, CallResult | None]:
        self.now = AFTER_HOURS if sc.after_hours else IN_HOURS
        self.faults.stt_fail_calls = set(sc.stt_fail_calls)
        self.faults.llm_fail = sc.llm_down
        self.stub.statuses = list(sc.webhook_statuses)
        tenant = "tidewaterquint" if sc.did != ASHGROVE else "ashgrovepell"
        self.staff[tenant].mode = sc.staff_mode
        before = len(self.results)
        def busy() -> bool:  # the line's own view: still speaking, or working out its answer
            return any(x.caller == sc.caller and (x.ctx.bot_speaking or x.ctx.turn_busy) for x in self.server.sessions.values())

        played = await play(sc, self.server.port, self.caller_stack, self.staff[tenant], busy=busy)
        result = None
        end = asyncio.get_running_loop().time() + wait_for_record
        while asyncio.get_running_loop().time() < end:
            fresh = [r for r in self.results[before:] if r.record["caller_number"] == sc.caller]
            if fresh:
                result = fresh[0]
                break
            await asyncio.sleep(0.1)
        return played, result
