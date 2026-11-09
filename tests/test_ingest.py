from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from metering.ingest import IngestError, ReadingWriter, parse_payload, parse_topic
from metering.models import Meter, Reading
from tenancy.db import tenant_context

pytestmark = pytest.mark.django_db

ROOT = "thermaledger"


def topic(tenant="acme-steel", site="PLANT-01", meter="MTR-0001"):
    return f"{ROOT}/{tenant}/{site}/{meter}/reading"


def payload(ts="2026-11-01T06:00:00Z", cumulative="18000.500", **extra):
    return json.dumps({"ts": ts, "cumulative": cumulative, **extra}).encode()


# --- parsing ---------------------------------------------------------------


def test_topic_is_parsed_into_tenant_site_meter():
    assert parse_topic(topic(), ROOT) == ("acme-steel", "PLANT-01", "MTR-0001")


@pytest.mark.parametrize(
    "bad",
    [
        "thermaledger/acme-steel/PLANT-01/reading",
        "thermaledger/acme-steel/PLANT-01/MTR-0001/status",
        "other/acme-steel/PLANT-01/MTR-0001/reading",
        "",
    ],
)
def test_malformed_topics_are_rejected(bad):
    with pytest.raises(IngestError):
        parse_topic(bad, ROOT)


def test_payload_is_parsed_into_a_reading():
    parsed = parse_payload(topic(), payload(), ROOT)
    assert parsed.cumulative == Decimal("18000.500")
    assert parsed.recorded_at == dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"cumulative": "1"}).encode(),
        json.dumps({"ts": "not-a-time", "cumulative": "1"}).encode(),
        json.dumps({"ts": "2026-11-01T06:00:00Z"}).encode(),
        json.dumps({"ts": "2026-11-01T06:00:00Z", "cumulative": "abc"}).encode(),
        json.dumps({"ts": "2026-11-01T06:00:00Z", "cumulative": "-5"}).encode(),
    ],
)
def test_malformed_payloads_are_rejected(body):
    with pytest.raises(IngestError):
        parse_payload(topic(), body, ROOT)


def test_a_device_claiming_another_meters_serial_is_rejected():
    """The topic says MTR-0001; the body says MTR-9999. One of them is lying."""
    with pytest.raises(IngestError, match="does not match"):
        parse_payload(topic(), payload(meter="MTR-9999"), ROOT)


def test_an_unknown_quality_flag_is_downgraded_to_suspect():
    parsed = parse_payload(topic(), payload(quality="excellent"), ROOT)
    assert parsed.quality == Reading.QUALITY_SUSPECT


# --- writing ---------------------------------------------------------------


def test_a_valid_message_is_stored_against_the_right_tenant(tenant_a):
    writer = ReadingWriter()
    reading = writer.handle_message(topic(), payload(), ROOT)
    assert reading is not None
    assert writer.stats.accepted == 1
    with tenant_context(tenant_a["tenant"].id):
        stored = Reading.objects.get(pk=reading.pk)
        assert stored.tenant_id == tenant_a["tenant"].id
        assert stored.cumulative == Decimal("18000.500")


def test_a_replayed_message_is_a_duplicate_not_a_second_reading(tenant_a):
    """MQTT QoS 1 is at-least-once, so the same reading will arrive twice."""
    writer = ReadingWriter()
    writer.handle_message(topic(), payload(), ROOT)
    writer.handle_message(topic(), payload(), ROOT)
    assert writer.stats.accepted == 1
    assert writer.stats.duplicates == 1
    with tenant_context(tenant_a["tenant"].id):
        assert (
            Reading.objects.filter(
                recorded_at=dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)
            ).count()
            == 1
        )


def test_a_message_for_an_unknown_tenant_is_rejected(tenant_a):
    writer = ReadingWriter()
    assert writer.handle_message(topic(tenant="not-a-customer"), payload(), ROOT) is None
    assert writer.stats.rejected == 1


def test_a_message_for_an_unknown_meter_is_rejected(tenant_a):
    writer = ReadingWriter()
    assert writer.handle_message(topic(meter="MTR-NOPE"), payload(), ROOT) is None
    assert writer.stats.rejected == 1


def test_a_meter_serial_from_another_tenant_cannot_be_written_to(both):
    """Both tenants have a meter called MTR-0001. A message on Acme's topic
    must land on Acme's meter, never on Borax's identically-named one."""
    a, b = both
    writer = ReadingWriter()
    reading = writer.handle_message(topic(tenant="acme-steel"), payload(), ROOT)
    assert reading is not None
    with tenant_context(a["tenant"].id):
        assert Reading.objects.get(pk=reading.pk).meter_id == a["meter"].id
    with tenant_context(b["tenant"].id):
        assert not Reading.objects.filter(
            recorded_at=dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)
        ).exists()


def test_a_quarantined_meter_stops_accepting_readings(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tenant_a["meter"].status = Meter.STATUS_QUARANTINED
        tenant_a["meter"].save(update_fields=["status"])
    writer = ReadingWriter()
    assert writer.handle_message(topic(), payload(), ROOT) is None
    assert writer.stats.rejected == 1


def test_one_bad_message_does_not_stop_the_stream(tenant_a):
    writer = ReadingWriter()
    writer.handle_message(topic(), b"garbage", ROOT)
    reading = writer.handle_message(topic(), payload(), ROOT)
    assert reading is not None
    assert writer.stats.rejected == 1
    assert writer.stats.accepted == 1
