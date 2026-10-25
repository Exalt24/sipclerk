import sys
from datetime import datetime
from pathlib import Path

import pytest

from sipclerk.tenants import TenantRegistry

ROOT = Path(__file__).resolve().parent.parent
# Tuesday 2026-10-27 17:30 UTC is 10:30 in Los Angeles and 13:30 in Miami: both firms are open.
IN_HOURS = datetime(2026, 10, 27, 17, 30)
# Sunday 2026-11-01 03:00 UTC: both are closed.
AFTER_HOURS = datetime(2026, 11, 1, 3, 0)


@pytest.fixture(scope="session", autouse=True)
def _quiet_pipecat():
    from sipclerk.voice.models import quiet_pipecat_logs

    quiet_pipecat_logs()


@pytest.fixture(scope="session")
def registry() -> TenantRegistry:
    return TenantRegistry.from_dir(ROOT / "tenants")


@pytest.fixture
def ashgrove(registry):
    return registry.by_id("ashgrovepell")


@pytest.fixture
def tidewater(registry):
    return registry.by_id("tidewaterquint")


@pytest.fixture(autouse=True)
def _forget_recent_calls():
    vs = sys.modules.get("voice_support")
    if vs is not None:
        vs.RECENT.clear()
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """When a voice test fails, print what was said on the calls it used: the first thing anyone asks."""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    voice_support = sys.modules.get("voice_support")
    if voice_support is None:
        return
    lines = []
    for name, (played, result) in voice_support.RECENT:
        lines.append(f"--- {name}: sip {played.status}, onsets {[round(x) for x in played.onsets_ms]}, no_response {played.no_response}")
        if result:
            lines += [f"    {t['speaker']:6} {t['text']}" for t in result.transcript]
            lines.append(f"    outcome={result.record['outcome']} escalation={result.record.get('escalation')} "
                         f"events={[(e['kind'], e.get('reason') or e.get('rule') or '') for e in result.record['events']]}")
    if lines:
        report.sections.append(("calls used by this test", "\n".join(lines)))
