"""
Tamper-evident audit log, one hash chain per tenant.

Carried over from AgentGuard. The point is not that the log cannot be edited -
anyone with the database can edit it - but that an edit cannot be made to go
unnoticed: every later row's hash is computed over the previous row's hash, so
changing row N invalidates N+1..end, and `verify_chain` finds the break.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from django.db import models

from tenancy.models import TenantOwnedModel

GENESIS = "0" * 64


def canonical(payload: dict) -> str:
    """Stable JSON so the same facts always hash to the same digest."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(prev_hash: str, action: str, payload: dict) -> str:
    material = f"{prev_hash}|{action}|{canonical(payload)}".encode()
    return hashlib.sha256(material).hexdigest()


class AuditEvent(TenantOwnedModel):
    ACTION_READING_INGESTED = "reading.ingested"
    ACTION_INVOICE_ISSUED = "invoice.issued"
    ACTION_INVOICE_REPLAYED = "invoice.replayed"
    ACTION_INVOICE_VOIDED = "invoice.voided"
    ACTION_ADJUSTMENT_APPLIED = "adjustment.applied"
    ACTION_TARIFF_CHANGED = "tariff.changed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    sequence = models.BigIntegerField()
    action = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    actor = models.CharField(max_length=200, blank=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "sequence"], name="uniq_audit_sequence_per_tenant"
            )
        ]
        ordering = ["tenant_id", "sequence"]
        indexes = [models.Index(fields=["tenant", "action"], name="idx_audit_action")]

    def __str__(self):
        return f"#{self.sequence} {self.action}"

    @classmethod
    def record(cls, tenant_id, action: str, payload: dict, actor: str = "system"):
        """Append one event. Must be called inside the tenant's context.

        select_for_update on the tail serialises concurrent appends, so two
        billing runs cannot both claim the same sequence number and fork the
        chain.
        """
        tail = (
            cls.objects.select_for_update()
            .filter(tenant_id=tenant_id)
            .order_by("-sequence")
            .first()
        )
        prev_hash = tail.hash if tail else GENESIS
        sequence = (tail.sequence + 1) if tail else 1
        return cls.objects.create(
            tenant_id=tenant_id,
            sequence=sequence,
            action=action,
            payload=payload,
            actor=actor,
            prev_hash=prev_hash,
            hash=digest(prev_hash, action, payload),
        )

    @classmethod
    def verify_chain(cls, tenant_id) -> tuple[bool, int, str | None]:
        """Walk the chain. Returns (ok, events_checked, first_bad_event_id)."""
        prev_hash = GENESIS
        checked = 0
        expected_sequence = 1
        for event in cls.objects.filter(tenant_id=tenant_id).order_by("sequence"):
            if event.sequence != expected_sequence:
                return False, checked, str(event.id)
            if event.prev_hash != prev_hash:
                return False, checked, str(event.id)
            if event.hash != digest(event.prev_hash, event.action, event.payload):
                return False, checked, str(event.id)
            prev_hash = event.hash
            expected_sequence += 1
            checked += 1
        return True, checked, None
