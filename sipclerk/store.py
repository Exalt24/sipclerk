"""Per-tenant storage of call records, transcripts, summaries and recordings, with controlled access.

* Everything at rest is AES-256-GCM encrypted under a key derived per tenant (HKDF from one master key
  and the tenant id), with the tenant, call and part bound in as associated data. Another tenant's
  key cannot open the file, and a file moved to another call's folder fails to decrypt.
* Reading goes through `read(actor, ...)`: the actor must belong to the tenant and hold a role the
  tenant's config grants for that part. Every read, allowed or denied, writes one audit line.
* The audit log is hash-chained (each line carries the hash of the one before), so an edited or
  deleted line is detectable with `verify_audit`.
* `purge` deletes calls older than the tenant's `retention_days` and audits that too.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import wave
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from sipclerk.tenants import TenantRegistry

PARTS = ("record", "transcript", "summary", "recording")
CHAIN_START = "0" * 64


log = logging.getLogger("sipclerk.store")


class AccessDenied(PermissionError):
    pass


class PurgeResult(list):
    """The (tenant, call) pairs a purge deleted, as a plain list; `.skipped` holds (tenant, call, reason) for what it could not handle."""

    def __init__(self, *args):
        super().__init__(*args)
        self.skipped: list[tuple[str, str, str]] = []


@dataclass(frozen=True)
class Actor:
    name: str
    tenant_id: str
    role: str


def derive_key(master: bytes, tenant_id: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"sipclerk-at-rest:" + tenant_id.encode()).derive(master)


def encrypt(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


def pcm_to_wav_bytes(pcm16_8k: bytes) -> bytes:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(pcm16_8k)
    return buf.getvalue()


class TenantStore:
    def __init__(self, root: Path, registry: TenantRegistry, master_key: bytes, clock: Callable[[], datetime] = datetime.utcnow):
        if len(master_key) < 32:
            raise ValueError("master key must be at least 32 bytes")
        self.root = Path(root)
        self.registry = registry
        self._master = master_key
        self.clock = clock

    # ---- paths

    def _tenant_dir(self, tenant_id: str) -> Path:
        if self.registry.by_id(tenant_id) is None:
            raise KeyError(f"unknown tenant {tenant_id}")
        return self.root / "tenants" / tenant_id

    def _call_dir(self, tenant_id: str, call_id: str) -> Path:
        if not call_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("bad call id")
        return self._tenant_dir(tenant_id) / "calls" / call_id

    def _key(self, tenant_id: str) -> bytes:
        return derive_key(self._master, tenant_id)

    @staticmethod
    def _aad(tenant_id: str, call_id: str, part: str) -> bytes:
        return f"{tenant_id}|{call_id}|{part}".encode()

    # ---- write

    def save_call(self, tenant_id: str, call_id: str, record: dict, transcript: list[dict], summary: str,
                  recording_pcm8k: bytes | None) -> list[str]:
        d = self._call_dir(tenant_id, call_id)
        d.mkdir(parents=True, exist_ok=True)
        key = self._key(tenant_id)
        parts: dict[str, bytes] = {
            "record": json.dumps(record, sort_keys=True).encode(),
            "transcript": json.dumps(transcript).encode(),
            "summary": summary.encode(),
        }
        if recording_pcm8k:
            parts["recording"] = pcm_to_wav_bytes(recording_pcm8k)
        for part, data in parts.items():
            (d / f"{part}.enc").write_bytes(encrypt(key, data, self._aad(tenant_id, call_id, part)))
        meta = {"call_id": call_id, "saved_at": self.clock().isoformat() + "Z", "outcome": record.get("outcome", ""),
                "parts": sorted(parts)}
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        self._audit(tenant_id, "system", "system", "write", call_id, ",".join(sorted(parts)), "ok")
        return sorted(parts)

    # ---- read (controlled)

    def _may_read(self, actor: Actor, tenant_id: str, part: str) -> bool:
        cfg = self.registry.by_id(tenant_id).config
        if actor.tenant_id != tenant_id:
            return False
        if part == "recording":
            return actor.role in cfg.recording_roles
        return actor.role in cfg.staff_roles

    def read(self, actor: Actor, tenant_id: str, call_id: str, part: str):
        if part not in PARTS:
            raise ValueError(f"unknown part {part}")
        if not self._may_read(actor, tenant_id, part):
            self._audit(tenant_id, actor.name, actor.role, "read", call_id, part, f"denied (actor tenant {actor.tenant_id})")
            raise AccessDenied(f"{actor.name} may not read {part} for {tenant_id}")
        f = self._call_dir(tenant_id, call_id) / f"{part}.enc"
        if not f.exists():
            self._audit(tenant_id, actor.name, actor.role, "read", call_id, part, "missing")
            raise FileNotFoundError(f"{part} for {call_id}")
        data = decrypt(self._key(tenant_id), f.read_bytes(), self._aad(tenant_id, call_id, part))
        self._audit(tenant_id, actor.name, actor.role, "read", call_id, part, "ok")
        if part in ("record", "transcript"):
            return json.loads(data)
        return data if part == "recording" else data.decode()

    def list_calls(self, tenant_id: str) -> list[str]:
        base = self._tenant_dir(tenant_id) / "calls"
        return sorted(p.name for p in base.iterdir()) if base.exists() else []

    # ---- retention

    def purge(self, now: datetime | None = None) -> PurgeResult:
        """Delete every call older than its firm's `retention_days`. One folder that cannot be read, deleted or audited is
        skipped, logged and counted in `result.skipped`; it never stops the folders after it or the other firm."""
        now = now or self.clock()
        result = PurgeResult()
        for t in self.registry.all:
            tenant_id = t.config.tenant_id
            cutoff = now - timedelta(days=t.config.retention_days)
            try:
                call_ids = self.list_calls(tenant_id)
            except Exception as exc:  # one firm's folder that will not open is not the other firm's problem
                log.warning("retention: cannot list the calls of %s: %s", tenant_id, exc)
                result.skipped.append((tenant_id, "", f"list failed: {type(exc).__name__}: {exc}"))
                continue
            for call_id in call_ids:
                try:
                    meta_path = self._call_dir(tenant_id, call_id) / "meta.json"
                    saved = datetime.fromisoformat(json.loads(meta_path.read_text(encoding="utf-8"))["saved_at"].rstrip("Z"))
                except Exception as exc:  # a half written folder, a torn or foreign meta file, a bad folder name
                    log.warning("retention: skipped %s/%s, cannot read its meta.json: %s", tenant_id, call_id, exc)
                    result.skipped.append((tenant_id, call_id, f"unreadable meta: {type(exc).__name__}: {exc}"))
                    continue
                if saved >= cutoff:
                    continue
                try:
                    shutil.rmtree(self._call_dir(tenant_id, call_id))
                except OSError as exc:
                    log.warning("retention: could not delete %s/%s: %s", tenant_id, call_id, exc)
                    result.skipped.append((tenant_id, call_id, f"delete failed: {type(exc).__name__}: {exc}"))
                    continue
                result.append((tenant_id, call_id))
                try:
                    self._audit(tenant_id, "system", "system", "purge", call_id, "all", f"older than {t.config.retention_days} days")
                except Exception as exc:  # the calls are gone: say so, and go on with the rest
                    log.error("retention: deleted %s/%s but could not write its audit line: %s", tenant_id, call_id, exc)
        return result

    # ---- audit

    def _audit_path(self, tenant_id: str) -> Path:
        return self._tenant_dir(tenant_id) / "audit.jsonl"

    def _audit(self, tenant_id: str, actor: str, role: str, action: str, call_id: str, part: str, outcome: str) -> None:
        p = self._audit_path(tenant_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        prev = CHAIN_START
        if p.exists():
            lines = p.read_text(encoding="utf-8").splitlines()
            if lines:
                prev = json.loads(lines[-1])["hash"]
        entry = {"at": self.clock().isoformat() + "Z", "actor": actor, "role": role, "action": action, "call_id": call_id,
                 "part": part, "outcome": outcome, "prev": prev}
        entry["hash"] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest()
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def audit_lines(self, tenant_id: str) -> list[dict]:
        p = self._audit_path(tenant_id)
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def verify_audit(self, tenant_id: str) -> bool:
        prev = CHAIN_START
        try:
            entries = self.audit_lines(tenant_id)
        except ValueError:  # a line that is not JSON is a damaged chain, which is what this method exists to say
            return False
        for e in entries:
            body = {k: v for k, v in e.items() if k != "hash"}
            if e["prev"] != prev or hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != e["hash"]:
                return False
            prev = e["hash"]
        return True
