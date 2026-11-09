"""
Transport-independent ingestion core.

Kept separate from the MQTT client so the tests can exercise it without a
broker, and so a second transport (an HTTP push from an IoT platform, say)
would reuse the same validation, idempotency and tenant binding.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from tenancy.db import tenant_context
from tenancy.models import Tenant

from .models import Meter, Reading

log = logging.getLogger(__name__)


class IngestError(ValueError):
    """Payload could not be turned into a reading."""


@dataclasses.dataclass(frozen=True)
class ParsedReading:
    tenant_slug: str
    site_code: str
    meter_serial: str
    recorded_at: dt.datetime
    cumulative: Decimal
    quality: str


@dataclasses.dataclass
class IngestStats:
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0

    def as_dict(self):
        return dataclasses.asdict(self)


def parse_topic(topic: str, root: str) -> tuple[str, str, str]:
    """`<root>/<tenant>/<site>/<meter>/reading` -> (tenant, site, meter)."""
    parts = topic.strip("/").split("/")
    if len(parts) != 5 or parts[0] != root or parts[4] != "reading":
        raise IngestError(f"unexpected topic {topic!r}")
    return parts[1], parts[2], parts[3]


def parse_payload(topic: str, payload: bytes, root: str) -> ParsedReading:
    tenant_slug, site_code, meter_serial = parse_topic(topic, root)
    try:
        body = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise IngestError(f"payload is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise IngestError("payload is not a JSON object")

    raw_ts = body.get("ts")
    if not raw_ts:
        raise IngestError("payload has no 'ts'")
    try:
        recorded_at = dt.datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
    except ValueError as exc:
        raise IngestError(f"bad 'ts': {exc}") from exc
    if timezone.is_naive(recorded_at):
        recorded_at = timezone.make_aware(recorded_at, dt.timezone.utc)

    raw_value = body.get("cumulative", body.get("cumulative_kwh"))
    if raw_value is None:
        raise IngestError("payload has no 'cumulative'")
    try:
        cumulative = Decimal(str(raw_value))
    except (InvalidOperation, ValueError) as exc:
        raise IngestError(f"bad 'cumulative': {exc}") from exc
    if cumulative < 0:
        raise IngestError("'cumulative' is negative")

    quality = str(body.get("quality", Reading.QUALITY_GOOD)).lower()
    if quality not in dict(Reading.QUALITY_CHOICES):
        quality = Reading.QUALITY_SUSPECT

    # The meter serial the device claims in the topic is cross-checked against
    # the serial in the payload when present; a mismatch is a device trying to
    # write as another device.
    claimed = body.get("meter")
    if claimed and str(claimed) != meter_serial:
        raise IngestError(
            f"payload meter {claimed!r} does not match topic meter {meter_serial!r}"
        )

    return ParsedReading(
        tenant_slug=tenant_slug,
        site_code=site_code,
        meter_serial=meter_serial,
        recorded_at=recorded_at,
        cumulative=cumulative,
        quality=quality,
    )


class ReadingWriter:
    """Resolves tenants/meters once and writes readings under tenant context."""

    def __init__(self):
        self._tenants: dict[str, Tenant] = {}
        self._meters: dict[tuple[str, str, str], Meter] = {}
        self.stats = IngestStats()

    def _tenant(self, slug: str) -> Tenant:
        if slug not in self._tenants:
            tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
            if tenant is None:
                raise IngestError(f"unknown or inactive tenant {slug!r}")
            self._tenants[slug] = tenant
        return self._tenants[slug]

    def _meter(self, tenant: Tenant, site_code: str, serial: str) -> Meter:
        key = (tenant.slug, site_code, serial)
        if key not in self._meters:
            meter = (
                Meter.objects.select_related("site")
                .filter(site__code=site_code, serial=serial)
                .first()
            )
            if meter is None:
                raise IngestError(f"unknown meter {serial!r} at site {site_code!r}")
            if meter.status != Meter.STATUS_ACTIVE:
                raise IngestError(f"meter {serial!r} is {meter.status}, not accepting readings")
            self._meters[key] = meter
        return self._meters[key]

    def write(self, parsed: ParsedReading) -> Reading | None:
        """Write one reading. Returns None if it was a duplicate."""
        tenant = self._tenant(parsed.tenant_slug)
        with tenant_context(tenant.id):
            meter = self._meter(tenant, parsed.site_code, parsed.meter_serial)
            with transaction.atomic():
                reading, created = Reading.objects.get_or_create(
                    meter=meter,
                    recorded_at=parsed.recorded_at,
                    defaults={
                        "tenant_id": tenant.id,
                        "cumulative": parsed.cumulative,
                        "quality": parsed.quality,
                    },
                )
        if created:
            self.stats.accepted += 1
            return reading
        self.stats.duplicates += 1
        return None

    def handle_message(self, topic: str, payload: bytes, root: str) -> Reading | None:
        try:
            parsed = parse_payload(topic, payload, root)
        except IngestError as exc:
            self.stats.rejected += 1
            log.warning("rejected message on %s: %s", topic, exc)
            return None
        try:
            return self.write(parsed)
        except IngestError as exc:
            self.stats.rejected += 1
            log.warning("rejected reading on %s: %s", topic, exc)
            return None
