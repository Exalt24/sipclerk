"""The inbound line: SIP INVITEs are routed to a tenant by the dialled number and answered by a CallSession."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from sipclerk import audio, delivery, guard
from sipclerk.call import CallResult, CallSession, Faults
from sipclerk.delivery import Outbox, Target
from sipclerk.intake.machine import IntakeMachine
from sipclerk.sip import message as sm
from sipclerk.sip.rtp import RtpSession
from sipclerk.sip.stack import IncomingInvite, SipStack
from sipclerk.store import TenantStore
from sipclerk.tenants import TenantRegistry
from sipclerk.voice.llm import Llm
from sipclerk.voice.models import Models

log = logging.getLogger("sipclerk.server")
PREFERRED_CODECS = [audio.PCMU, audio.PCMA]
RETENTION_EVERY_S = 3600.0  # how often the line applies each firm's retention_days: at start, then every hour


class ClerkServer:
    def __init__(self, *, registry: TenantRegistry, models: Models, store: TenantStore, outbox: Outbox, host: str = "0.0.0.0",
                 port: int = 5060, advertise_host: str | None = None, rtp_host: str = "0.0.0.0", max_calls: int = 8,
                 now_fn: Callable[[], datetime] = datetime.utcnow, llm_factory: Callable[[], Llm] = Llm,
                 faults: Faults | None = None, on_finished: Callable[[CallResult], None] | None = None,
                 retention_every_s: float = RETENTION_EVERY_S):
        if retention_every_s <= 0:
            raise ValueError("retention_every_s must be above zero")
        self.registry = registry
        self.models = models
        self.store = store
        self.outbox = outbox
        self.stack = SipStack(host, port, self.on_invite, advertise_host)
        self.rtp_host = rtp_host
        self.max_calls = max_calls
        self.now_fn = now_fn
        self.llm_factory = llm_factory
        self.faults = faults or Faults()
        self.on_finished = on_finished
        self.sessions: dict[str, CallSession] = {}
        self._answering: dict[str, tuple[str, str]] = {}  # SIP Call-ID -> (caller, firm) of an INVITE being answered, before its ACK makes it a session
        self.results: list[CallResult] = []
        self.counters = {"answered": 0, "wrong_number": 0, "blocked_caller": 0, "duplicate_call": 0, "busy": 0, "no_codec": 0,
                         "no_offer": 0, "no_ack": 0}
        self.retention_every_s = retention_every_s
        self.retention = {"passes": 0, "failures": 0, "calls_purged": 0, "folders_skipped": 0, "outbox_entries_purged": 0, "last_ok_at": None, "audit_broken": []}
        self._stop = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._retention_task: asyncio.Task | None = None

    @property
    def port(self) -> int:
        return self.stack.port

    async def start(self) -> None:
        await self.stack.start()
        self.run_retention_pass()  # a line that was down for a month must not keep expired calls until the first interval
        loop = asyncio.get_running_loop()
        self._worker = loop.create_task(self.outbox.run(self._stop))
        self._retention_task = loop.create_task(self._retention_loop())

    async def stop(self) -> None:
        for s in list(self.sessions.values()):
            s.finished.set()
        await asyncio.sleep(0.05)
        self._stop.set()
        if self._worker:
            await self._worker
        if self._retention_task:
            await self._retention_task
        await self.stack.stop()

    def status(self) -> dict:
        """What an operator looks at: call counters, deliveries (the dead letters among them) and the retention passes."""
        return {"calls_open": len(self.sessions), "counters": dict(self.counters), "deliveries": {**self.outbox.stats(), "waiting": len(self.outbox.pending)},
                "retention": dict(self.retention)}

    def file_call(self, cfg, record: dict, transcript: list[dict], summary: str, rec_pcm: bytes | None, duration_s: int) -> list[str]:
        """Store a finished call and queue it for its receivers: the one way every call ends up in the record and in the outbox, whether it was a complete
        intake, a hang up, a failed transfer or a dialog that never got its ACK."""
        parts = self.store.save_call(cfg.tenant_id, record["call_id"], record, transcript, summary, rec_pcm)
        delivery.queue_call(self.outbox, cfg, record, transcript, duration_s, summary)
        asyncio.get_running_loop().create_task(self.outbox.deliver_due())
        return parts

    # ---- retention

    def run_retention_pass(self) -> bool:
        """Delete every call older than its firm's retention_days, then check each firm's audit chain. A failing pass is logged and
        counted, never raised: the next interval tries again."""
        try:
            purged = self.store.purge(self.now_fn())
        except Exception:
            self.retention["failures"] += 1
            log.exception("retention pass failed; it will be tried again in %.0f s", self.retention_every_s)
            return False
        self.retention["passes"] += 1
        self.retention["last_ok_at"] = self.now_fn().isoformat()
        self.retention["calls_purged"] += len(purged)
        self.retention["folders_skipped"] = len(purged.skipped)
        if purged or purged.skipped:
            log.info("retention: deleted %d call(s), skipped %d folder(s) it could not read", len(purged), len(purged.skipped))
        try:  # a delivered or dead outbox entry goes at its firm's retention like a call does
            self.retention["outbox_entries_purged"] += self.outbox.purge(self._retention_days)
        except Exception:
            log.exception("retention: the outbox purge failed; it will be tried again in %.0f s", self.retention_every_s)
        if self.outbox.dead:  # a delivery given up on is the one thing here that needs a person: keep saying so until it is sent again or purged
            log.warning("outbox: %d dead delivery(ies); `python scripts/requeue_dead.py` sends them again", len(self.outbox.dead))
        self.retention["audit_broken"] = [t.config.tenant_id for t in self.registry.all if not self._audit_verifies(t.config.tenant_id)]
        if self.retention["audit_broken"]:
            log.error("retention: the audit chain of %s does not verify", ", ".join(self.retention["audit_broken"]))
        log.info("line status: %s", self.status())  # counts only, once an hour
        return True

    def _retention_days(self, tenant_id: str) -> int | None:
        t = self.registry.by_id(tenant_id)
        return t.config.retention_days if t else None

    def _audit_verifies(self, tenant_id: str) -> bool:
        try:
            return self.store.verify_audit(tenant_id)
        except Exception:  # a line that is not even JSON is a broken chain
            return False

    async def _retention_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), self.retention_every_s)
                return
            except asyncio.TimeoutError:
                pass
            self.run_retention_pass()

    def _already_on_this_line(self, caller: str, tenant_id: str) -> bool:
        """A caller who is already in a call on this firm's line. A withheld caller id is nobody in particular: two anonymous callers are two calls.
        (A retransmitted INVITE never gets here: the stack answers it from the first one's response.)"""
        if sm.caller_is_withheld(caller):
            return False
        open_here = [s.caller for s in self.sessions.values() if s.cfg.tenant_id == tenant_id]
        open_here += [c for c, t in self._answering.values() if t == tenant_id]  # answered and waiting for the ACK: already on the line
        return bool(sm.match_number(caller, [c for c in open_here if not sm.caller_is_withheld(c)]))

    async def on_invite(self, inv: IncomingInvite) -> None:
        tenant = self.registry.resolve(inv.candidates)
        if tenant is None:  # a wrong number: nothing is loaded and nothing is recorded
            self.counters["wrong_number"] += 1
            inv.reject(404, "Not Found")
            return
        v = guard.check_caller(inv.caller, tenant.config)
        if v.action == "blocked_caller":
            self.counters["blocked_caller"] += 1
            inv.reject(603, "Decline")
            return
        if self._already_on_this_line(inv.caller, tenant.config.tenant_id):
            self.counters["duplicate_call"] += 1  # the same caller on the same line already has a call open
            inv.reject(486, "Busy Here")
            return
        if len(self.sessions) >= self.max_calls:
            self.counters["busy"] += 1
            inv.reject(503, "Service Unavailable")
            return
        if not inv.offer.strip():  # a late-offer INVITE (no SDP) is not supported
            self.counters["no_offer"] += 1
            inv.reject(488, "Not Acceptable Here")
            return
        try:
            offer = sm.parse_sdp(inv.offer)
        except sm.SipParseError:
            inv.reject(400, "Bad SDP")
            return
        pick = sm.negotiate(offer, PREFERRED_CODECS)
        if pick is None:
            self.counters["no_codec"] += 1
            inv.reject(488, "Not Acceptable Here")
            return
        codec, _offered_pt = pick
        self._answering[inv.req.call_id] = (inv.caller, tenant.config.tenant_id)  # from here to the session, with nothing in between that can yield
        try:
            await self._take_call(inv, tenant, offer, codec)
        finally:
            self._answering.pop(inv.req.call_id, None)

    async def _take_call(self, inv: IncomingInvite, tenant, offer, codec: str) -> None:
        """Answer the INVITE, wait for its ACK and start the call."""
        rtp = await RtpSession.open(self.rtp_host, 0, codec, offer.dtmf_pt)
        rtp.set_remote(offer.host, offer.port)  # replaced by the first packet's source if the caller is behind NAT
        inv.provisional(180, "Ringing")
        host = self.stack.advertise_host or (inv.dialled_ip if self.stack.host in ("0.0.0.0", "") and inv.dialled_ip else self.stack.local_host_for(inv.addr))
        dialog = await inv.answer(sm.build_sdp(host, rtp.local_port, [codec], offer.dtmf_pt), local_host=host)
        got = await inv.wait_for_ack()  # as long as the stack still takes an ACK: a lost or late ACK is ordinary over UDP and a carrier
        if got != "acked":
            await rtp.close()
            if got == "lost":
                await self._record_unacknowledged_call(tenant, inv, codec)
            return
        rtp.start()
        call_id = secrets.token_hex(6)
        llm = self.llm_factory()
        llm.fail = llm.fail or self.faults.llm_fail
        session = CallSession(server=self, tenant=tenant, dialog=dialog, rtp=rtp, codec=codec, caller=inv.caller, call_id=call_id,
                              now_utc=self.now_fn(), models=self.models, llm=llm, stack=self.stack, faults=self.faults)
        self.sessions[call_id] = session
        self.counters["answered"] += 1
        asyncio.get_running_loop().create_task(self._run(call_id, session))

    async def _record_unacknowledged_call(self, tenant, inv: IncomingInvite, codec: str) -> None:
        """The line answered and no ACK ever came, so the stack ended the session with a BYE. The firm still gets what it would for any call that ends
        without an intake: a stored record with a callback task, and the same deliveries."""
        self.counters["no_ack"] += 1
        call_id = secrets.token_hex(6)
        m = IntakeMachine(tenant, call_id, inv.caller, self.now_fn())
        m.on_no_ack()
        record = m.record()
        record.update({"ended_by": "no-ack", "duration_s": 0.0, "codec": codec})
        cfg = tenant.config
        summary = f"A call from this number was answered for {cfg.display_name} but never acknowledged, so no intake was taken. A callback task is open."
        try:
            self.file_call(cfg, record, [], summary, None, 0)
        except Exception:
            log.exception("call %s: the record of an unacknowledged call could not be stored", call_id)

    async def _run(self, call_id: str, session: CallSession) -> None:
        try:
            result = await session.run()
            self.results.append(result)
            if self.on_finished:
                self.on_finished(result)
        except Exception:
            log.exception("call %s failed", call_id)
        finally:
            self.sessions.pop(call_id, None)


def master_key_from_env() -> bytes:
    """The at-rest master key. A lab key is generated into .secrets once and reused; production would use a KMS."""
    env = os.environ.get("SIPCLERK_MASTER_KEY")
    if env:
        return bytes.fromhex(env)
    path = Path(__file__).resolve().parent.parent / ".secrets" / "master.key"
    if path.exists():
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    path.parent.mkdir(exist_ok=True)
    key = secrets.token_bytes(32)
    path.write_text(key.hex(), encoding="utf-8")
    return key
