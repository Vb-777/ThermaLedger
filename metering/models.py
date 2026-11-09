from __future__ import annotations

import uuid

from django.db import models

from tenancy.models import TenantOwnedModel


class Site(TenantOwnedModel):
    """An industrial plant where Clear Energy has installed metered equipment."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=200)
    address = models.TextField(blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    commissioned_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_site_code_per_tenant")
        ]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} - {self.name}"


class Meter(TenantOwnedModel):
    """A physical meter. `serial` is the CN of the device certificate used for
    mutual TLS on the MQTT broker, so the identity the broker authenticates and
    the identity rows are attributed to are the same string."""

    SERVICE_STEAM = "steam"
    SERVICE_THERMAL = "thermal"
    SERVICE_LPG = "lpg"
    SERVICE_CHOICES = [
        (SERVICE_STEAM, "Steam as a Service"),
        (SERVICE_THERMAL, "Thermal Energy as a Service"),
        (SERVICE_LPG, "Bulk LPG"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_QUARANTINED = "quarantined"
    STATUS_RETIRED = "retired"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_QUARANTINED, "Quarantined"),
        (STATUS_RETIRED, "Retired"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="meters")
    serial = models.CharField(max_length=64)
    service = models.CharField(max_length=16, choices=SERVICE_CHOICES)
    unit = models.CharField(max_length=16, default="kWh")
    multiplier = models.DecimalField(max_digits=10, decimal_places=4, default=1)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    installed_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "serial"], name="uniq_meter_serial_per_tenant"
            )
        ]
        ordering = ["serial"]

    def __str__(self):
        return self.serial


class Reading(TenantOwnedModel):
    """One cumulative meter reading.

    Cumulative rather than interval: a lost packet then costs you resolution,
    not kWh, because the next reading still carries the full total. Consumption
    is the difference between the bounding readings of a period - see
    billing/engine.py.
    """

    QUALITY_GOOD = "good"
    QUALITY_ESTIMATED = "estimated"
    QUALITY_SUSPECT = "suspect"
    QUALITY_CHOICES = [
        (QUALITY_GOOD, "Good"),
        (QUALITY_ESTIMATED, "Estimated"),
        (QUALITY_SUSPECT, "Suspect"),
    ]

    id = models.BigAutoField(primary_key=True)
    meter = models.ForeignKey(Meter, on_delete=models.CASCADE, related_name="readings")
    recorded_at = models.DateTimeField(db_index=True)
    cumulative = models.DecimalField(max_digits=16, decimal_places=3)
    quality = models.CharField(max_length=16, choices=QUALITY_CHOICES, default=QUALITY_GOOD)
    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Replay of the same reading is a no-op rather than a duplicate.
            # The ingestion path relies on this for at-least-once MQTT delivery.
            models.UniqueConstraint(
                fields=["meter", "recorded_at"], name="uniq_reading_per_meter_instant"
            )
        ]
        indexes = [
            models.Index(fields=["tenant", "meter", "recorded_at"], name="idx_reading_lookup"),
        ]
        ordering = ["recorded_at"]

    def __str__(self):
        return f"{self.meter_id} @ {self.recorded_at.isoformat()} = {self.cumulative}"
