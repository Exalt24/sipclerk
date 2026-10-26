import json
from datetime import datetime

import pytest
import yaml

from conftest import AFTER_HOURS, IN_HOURS, ROOT
from sipclerk import tenants as t


def _raw(name="ashgrovepell"):
    return yaml.safe_load((ROOT / "tenants" / f"{name}.yaml").read_text(encoding="utf-8"))


def test_both_firms_load_and_route_by_dialled_number(registry):
    assert registry.by_did("+14155550101").config.tenant_id == "ashgrovepell"
    assert registry.by_did("1 (305) 555-0102").config.tenant_id == "tidewaterquint"
    assert registry.by_did("+19995550000") is None


def test_the_two_firms_share_nothing_that_would_leak(registry):
    a, b = (registry.by_id(i).config for i in ("ashgrovepell", "tidewaterquint"))
    assert a.did != b.did and a.transfer.destination != b.transfer.destination
    assert {q.prompt for q in a.questions}.isdisjoint({q.prompt for q in b.questions})
    assert a.greeting != b.greeting and a.closing != b.closing
    assert {x.id for x in a.practice_areas}.isdisjoint({x.id for x in b.practice_areas})


def test_hash_follows_content_not_formatting(tmp_path):
    raw = _raw()
    assert t.canonical_hash(raw) == t.canonical_hash(json.loads(json.dumps(raw)))
    changed = _raw()
    changed["questions"][0]["prompt"] = "What is your name, first and last?"
    assert t.canonical_hash(changed) != t.canonical_hash(raw)
    f = tmp_path / "x.yaml"
    f.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert t.load_file(f).config_hash == t.canonical_hash(raw)


def test_stamp_carries_tenant_version_and_hash(ashgrove):
    s = ashgrove.stamp
    assert s["tenant_id"] == "ashgrovepell" and s["config_version"] == "2026.10.7" and len(s["config_hash"]) == 64


@pytest.mark.parametrize("edit,message", [
    (lambda r: r.update(unknown_field=1), "unknown_field"),
    (lambda r: r.update(did="12345"), "E.164"),
    (lambda r: r.update(schema_version=2), "schema_version"),
    (lambda r: r["questions"].append(dict(r["questions"][0])), "duplicate question id"),
    (lambda r: r["questions"][0].update(type="color"), "type"),
    (lambda r: r["qualification"][0].update(field="nope"), "unknown field"),
    (lambda r: r["hours"]["weekly"].update(mon=["17:00", "09:00"]), "bad hours"),
    (lambda r: r["hours"]["weekly"].update(funday=["09:00", "10:00"]), "unknown day"),
    (lambda r: r.update(tenant_id="Bad Id"), "tenant_id"),
    (lambda r: r.update(retention_days=0), "retention_days"),
    (lambda r: r["questions"][0].update(timeout_s=0), "timeout_s"),
])
def test_bad_configs_are_refused_with_a_reason(tmp_path, edit, message):
    raw = _raw()
    edit(raw)
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(t.TenantError) as e:
        t.load_file(f)
    assert message in str(e.value)


def test_a_tenant_without_a_practice_area_question_is_refused(tmp_path):
    raw = _raw()
    raw["questions"] = [q for q in raw["questions"] if q["id"] != "practice_area"]
    raw["qualification"] = []
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(t.TenantError, match="practice_area"):
        t.load_file(f)


def test_two_tenants_cannot_answer_the_same_number(tmp_path, registry):
    a = registry.by_id("ashgrovepell")
    clone_raw = _raw("tidewaterquint")
    clone_raw["did"] = a.config.did
    f = tmp_path / "clone.yaml"
    f.write_text(yaml.safe_dump(clone_raw), encoding="utf-8")
    with pytest.raises(t.TenantError, match="two tenants answer"):
        t.TenantRegistry([a, t.load_file(f)])


def test_an_empty_directory_is_an_error(tmp_path):
    with pytest.raises(t.TenantError):
        t.TenantRegistry.from_dir(tmp_path)


def test_opening_hours_follow_the_firms_own_clock(registry):
    a, b = registry.by_id("ashgrovepell").config, registry.by_id("tidewaterquint").config
    assert a.is_open(IN_HOURS) and b.is_open(IN_HOURS)
    assert not a.is_open(AFTER_HOURS) and not b.is_open(AFTER_HOURS)
    # Saturday 10:30 in Miami is inside Tidewater's Saturday window and outside Ashgrove's (no Saturday)
    sat = datetime(2026, 10, 31, 14, 30)
    assert b.is_open(sat) and not a.is_open(sat)
    # 08:59 and 09:00 on a Tuesday in Los Angeles
    assert not a.is_open(datetime(2026, 10, 27, 15, 59)) and a.is_open(datetime(2026, 10, 27, 16, 0))
    assert a.is_open(datetime(2026, 10, 27, 23, 59)) and not a.is_open(datetime(2026, 10, 28, 0, 0))


def test_json_schema_is_exported(tmp_path):
    out = tmp_path / "tenant.schema.json"
    t.export_json_schema(out)
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert "tenant_id" in schema["properties"] and schema["additionalProperties"] is False


def test_hash_does_not_depend_on_key_order():
    raw = _raw()
    reordered = dict(reversed(list(raw.items())))
    reordered["questions"] = [dict(reversed(list(q.items()))) for q in raw["questions"]]
    assert list(reordered) != list(raw) and t.canonical_hash(reordered) == t.canonical_hash(raw)


def test_the_committed_json_schema_is_the_one_the_models_generate():
    committed = json.loads((ROOT / "tenants" / "tenant.schema.json").read_text(encoding="utf-8"))
    assert committed == t.TenantConfig.model_json_schema()
