"""
Charge-once, and prove the ledger has not been edited.

Both mechanisms are carried over from earlier work - Paylink's idempotency
keys, AgentGuard's hash-chained log - applied here to invoice issuance.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from auditlog.models import GENESIS, AuditEvent, digest
from billing import engine
from billing.models import IdempotencyKey, Invoice
from tenancy.db import tenant_context

from .conftest import PERIOD_END, PERIOD_START

pytestmark = pytest.mark.django_db


# --- idempotency -----------------------------------------------------------


def test_rerunning_the_same_period_does_not_issue_a_second_invoice(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        first, created_first = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        second, created_second = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        count = Invoice.objects.filter(tenant_id=tenant_a["tenant"].id).count()

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert count == 1, "the same billing period produced two invoices"


def test_a_replay_is_recorded_rather_than_hidden(tenant_a):
    """A second run is a fact about operations; it should be visible."""
    with tenant_context(tenant_a["tenant"].id):
        engine.issue_invoice(tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END)
        engine.issue_invoice(tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END)
        actions = list(
            AuditEvent.objects.filter(tenant_id=tenant_a["tenant"].id)
            .order_by("sequence")
            .values_list("action", flat=True)
        )
    assert actions == [
        AuditEvent.ACTION_INVOICE_ISSUED,
        AuditEvent.ACTION_INVOICE_REPLAYED,
    ]


def test_the_idempotency_key_is_unique_per_tenant_and_period(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        key = engine.idempotency_key(tenant_a["site"].id, PERIOD_START, PERIOD_END)
        IdempotencyKey.objects.create(tenant_id=tenant_a["tenant"].id, key=key)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                IdempotencyKey.objects.create(tenant_id=tenant_a["tenant"].id, key=key)


def test_the_same_key_may_exist_in_two_tenants(both):
    """Keys are namespaced by tenant; Acme's October key must not block Borax's."""
    a, b = both
    key = "invoice:shared:2026-10-01:2026-11-01"
    with tenant_context(a["tenant"].id):
        IdempotencyKey.objects.create(tenant_id=a["tenant"].id, key=key)
    with tenant_context(b["tenant"].id):
        IdempotencyKey.objects.create(tenant_id=b["tenant"].id, key=key)  # must not raise


def test_a_different_period_does_issue_a_new_invoice(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        engine.issue_invoice(tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END)
        _, created = engine.issue_invoice(
            tenant_a["tenant"].id,
            tenant_a["site"],
            dt.date(2026, 11, 1),
            dt.date(2026, 12, 1),
        )
    assert created is True


def test_adjustments_are_consumed_exactly_once(tenant_a):
    from billing.models import Adjustment

    with tenant_context(tenant_a["tenant"].id):
        Adjustment.objects.create(
            tenant=tenant_a["tenant"],
            site=tenant_a["site"],
            kind=Adjustment.KIND_CREDIT,
            amount_paise=5_000,
            reason="meter fault credit",
        )
        first, _ = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        # Next period: the credit is already spent and must not reappear.
        second, _ = engine.issue_invoice(
            tenant_a["tenant"].id,
            tenant_a["site"],
            dt.date(2026, 11, 1),
            dt.date(2026, 12, 1),
        )
    assert first.adjustments_paise == -5_000
    assert second.adjustments_paise == 0


# --- audit chain -----------------------------------------------------------


def test_the_chain_starts_at_genesis_and_links_forward(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        one = AuditEvent.record(tenant_a["tenant"].id, "test.one", {"n": 1})
        two = AuditEvent.record(tenant_a["tenant"].id, "test.two", {"n": 2})
    assert one.prev_hash == GENESIS
    assert one.sequence == 1
    assert two.prev_hash == one.hash
    assert two.sequence == 2


def test_an_intact_chain_verifies(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        for n in range(10):
            AuditEvent.record(tenant_a["tenant"].id, "test.event", {"n": n})
        ok, checked, bad = AuditEvent.verify_chain(tenant_a["tenant"].id)
    assert ok is True
    assert checked == 10
    assert bad is None


def test_editing_a_payload_breaks_verification(tenant_a):
    """The whole point: a silent edit becomes a loud one."""
    with tenant_context(tenant_a["tenant"].id):
        for n in range(5):
            AuditEvent.record(tenant_a["tenant"].id, "test.event", {"amount_paise": n * 100})
        victim = AuditEvent.objects.filter(tenant_id=tenant_a["tenant"].id, sequence=3).get()
        victim.payload = {"amount_paise": 999_999}
        victim.save(update_fields=["payload"])
        ok, checked, bad = AuditEvent.verify_chain(tenant_a["tenant"].id)
    assert ok is False
    assert bad == str(victim.id)
    assert checked == 2, "verification should stop at the first broken link"


def test_deleting_an_event_breaks_verification(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        for n in range(5):
            AuditEvent.record(tenant_a["tenant"].id, "test.event", {"n": n})
        AuditEvent.objects.filter(tenant_id=tenant_a["tenant"].id, sequence=3).delete()
        ok, _, _ = AuditEvent.verify_chain(tenant_a["tenant"].id)
    assert ok is False


def test_rewriting_a_hash_to_match_still_breaks_the_next_link(tenant_a):
    """Recomputing the edited row's own hash is not enough - the row after it
    still carries the old prev_hash."""
    with tenant_context(tenant_a["tenant"].id):
        for n in range(4):
            AuditEvent.record(tenant_a["tenant"].id, "test.event", {"n": n})
        victim = AuditEvent.objects.filter(tenant_id=tenant_a["tenant"].id, sequence=2).get()
        victim.payload = {"n": 99}
        victim.hash = digest(victim.prev_hash, victim.action, victim.payload)
        victim.save(update_fields=["payload", "hash"])
        ok, checked, _ = AuditEvent.verify_chain(tenant_a["tenant"].id)
    assert ok is False
    assert checked == 2


def test_each_tenant_has_an_independent_chain(both):
    a, b = both
    with tenant_context(a["tenant"].id):
        AuditEvent.record(a["tenant"].id, "test.a", {"x": 1})
    with tenant_context(b["tenant"].id):
        event = AuditEvent.record(b["tenant"].id, "test.b", {"x": 1})
    assert event.sequence == 1, "tenant B's chain should not continue tenant A's numbering"
    assert event.prev_hash == GENESIS


def test_issuing_an_invoice_writes_the_figures_into_the_chain(tenant_a):
    """The audit event has to carry enough to reconstruct the bill, or it is
    decoration rather than evidence."""
    with tenant_context(tenant_a["tenant"].id):
        invoice, _ = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        event = AuditEvent.objects.filter(
            tenant_id=tenant_a["tenant"].id, action=AuditEvent.ACTION_INVOICE_ISSUED
        ).get()
    assert event.payload["number"] == invoice.number
    assert event.payload["total_paise"] == invoice.total_paise
    assert Decimal(event.payload["billed_units"]) == invoice.billed_units
    assert event.payload["period"] == [PERIOD_START.isoformat(), PERIOD_END.isoformat()]
