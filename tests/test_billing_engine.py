from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from billing import engine
from billing.models import Adjustment, Invoice, InvoiceLine, Tariff, TariffSlab
from metering.models import Meter, Reading
from tenancy.db import tenant_context

from .conftest import PERIOD_END, PERIOD_START

pytestmark = pytest.mark.django_db


def make_tariff(tenant, *, minimum=0, fixed=0, tax_bp=1800, bands=((None, 1000),)):
    tariff = Tariff.objects.create(
        tenant=tenant,
        name="test tariff",
        service=Meter.SERVICE_STEAM,
        fixed_charge_paise=fixed,
        minimum_offtake_units=Decimal(minimum),
        tax_percent_bp=tax_bp,
        effective_from=dt.date(2025, 1, 1),
    )
    for cap, rate in bands:
        TariffSlab.objects.create(
            tenant=tenant,
            tariff=tariff,
            upto_units=None if cap is None else Decimal(cap),
            rate_paise_per_unit=rate,
        )
    return tariff


# --- slab arithmetic -------------------------------------------------------


def test_single_band_is_rate_times_units(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], bands=((None, 800),), tax_bp=0)
        result = engine.compute(tariff, Decimal("100"), [])
    assert result.subtotal_paise == 80_000
    assert result.total_paise == 80_000


def test_consumption_is_split_across_bands(tenant_a):
    """6000 units over bands 0-5000 @ 8.20 and 5000+ @ 7.60."""
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(
            tenant_a["tenant"], bands=((5000, 820), (None, 760)), tax_bp=0
        )
        result = engine.compute(tariff, Decimal("6000"), [])
    slab_lines = [line for line in result.lines if line.kind == InvoiceLine.KIND_SLAB]
    assert [line.units for line in slab_lines] == [Decimal("5000.000"), Decimal("1000.000")]
    assert result.subtotal_paise == 5000 * 820 + 1000 * 760


def test_consumption_below_the_first_cap_uses_one_band_only(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], bands=((5000, 820), (None, 760)), tax_bp=0)
        result = engine.compute(tariff, Decimal("4000"), [])
    assert len([line for line in result.lines if line.kind == InvoiceLine.KIND_SLAB]) == 1
    assert result.subtotal_paise == 4000 * 820


def test_tariff_without_an_open_ended_band_is_an_error(tenant_a):
    """A tariff that cannot price the consumption must fail loudly.

    Silently dropping the unpriced units would under-bill the customer and the
    error would surface months later as a revenue gap.
    """
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], bands=((100, 820),), tax_bp=0)
        with pytest.raises(engine.BillingError):
            engine.compute(tariff, Decimal("500"), [])


# --- minimum offtake -------------------------------------------------------


def test_minimum_offtake_lifts_the_billed_units(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], minimum=1000, bands=((None, 800),), tax_bp=0)
        result = engine.compute(tariff, Decimal("250"), [])
    assert result.consumption_units == Decimal("250.000")
    assert result.billed_units == Decimal("1000.000")
    assert result.subtotal_paise == 1000 * 800
    assert any(line.kind == InvoiceLine.KIND_MINIMUM for line in result.lines), (
        "the invoice must say why the customer is paying for units they did not use"
    )


def test_minimum_offtake_does_not_reduce_real_consumption(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], minimum=1000, bands=((None, 800),), tax_bp=0)
        result = engine.compute(tariff, Decimal("2500"), [])
    assert result.billed_units == Decimal("2500.000")
    assert not any(line.kind == InvoiceLine.KIND_MINIMUM for line in result.lines)


# --- fixed charge, adjustments, tax ---------------------------------------


def test_fixed_charge_is_added_once(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], fixed=250_000, bands=((None, 800),), tax_bp=0)
        result = engine.compute(tariff, Decimal("100"), [])
    assert result.subtotal_paise == 80_000 + 250_000


def test_credit_adjustment_reduces_the_taxable_amount(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], bands=((None, 800),), tax_bp=1800)
        credit = Adjustment(
            tenant=tenant_a["tenant"],
            site=tenant_a["site"],
            kind=Adjustment.KIND_CREDIT,
            amount_paise=10_000,
            reason="goodwill",
        )
        result = engine.compute(tariff, Decimal("100"), [credit])
    assert result.adjustments_paise == -10_000
    assert result.tax_paise == engine.paise(Decimal(80_000 - 10_000) * Decimal("0.18"))
    assert result.total_paise == 70_000 + result.tax_paise


def test_tax_is_rounded_half_up_to_a_whole_paisa(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(tenant_a["tenant"], bands=((None, 1),), tax_bp=1850)
        result = engine.compute(tariff, Decimal("5"), [])
    # 5 paise subtotal, 18.5% = 0.925 paise -> 1 paise
    assert result.subtotal_paise == 5
    assert result.tax_paise == 1


def test_printed_lines_always_sum_to_the_printed_total(tenant_a):
    """The property a customer checks with a calculator."""
    with tenant_context(tenant_a["tenant"].id):
        tariff = make_tariff(
            tenant_a["tenant"],
            minimum=900,
            fixed=137_777,
            bands=((5000, 823), (None, 761)),
            tax_bp=1800,
        )
        debit = Adjustment(
            tenant=tenant_a["tenant"],
            site=tenant_a["site"],
            kind=Adjustment.KIND_DEBIT,
            amount_paise=4_321,
            reason="late fee",
        )
        for consumption in ("0", "1", "899.999", "5000", "5000.001", "12345.678"):
            result = engine.compute(tariff, Decimal(consumption), [debit])
            assert sum(line.amount_paise for line in result.lines) == result.total_paise, (
                f"lines do not add up to the total at consumption={consumption}"
            )


# --- consumption from cumulative readings ----------------------------------


def test_consumption_is_the_rise_across_the_period(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        consumed = engine.consumption_for_meter(
            tenant_a["meter"],
            dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
        )
    assert consumed == Decimal("7455.500")  # 31 rises of 240.5


def test_a_meter_reset_is_not_billed_as_negative_consumption(tenant_a):
    """A register rollover must not hand the customer a credit."""
    with tenant_context(tenant_a["tenant"].id):
        meter = Meter.objects.create(
            tenant=tenant_a["tenant"],
            site=tenant_a["site"],
            serial="MTR-RESET",
            service=Meter.SERVICE_STEAM,
        )
        for offset, value in enumerate(["900", "950", "20", "60"]):
            Reading.objects.create(
                tenant=tenant_a["tenant"],
                meter=meter,
                recorded_at=dt.datetime(2026, 10, 2, tzinfo=dt.timezone.utc)
                + dt.timedelta(hours=offset),
                cumulative=Decimal(value),
            )
        consumed = engine.consumption_for_meter(
            meter,
            dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
        )
    # 900->950 = 50, reset to 20 = 20, 20->60 = 40
    assert consumed == Decimal("110.000")


def test_a_meter_with_one_reading_consumes_nothing(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        meter = Meter.objects.create(
            tenant=tenant_a["tenant"],
            site=tenant_a["site"],
            serial="MTR-SINGLE",
            service=Meter.SERVICE_STEAM,
        )
        Reading.objects.create(
            tenant=tenant_a["tenant"],
            meter=meter,
            recorded_at=dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc),
            cumulative=Decimal("123"),
        )
        consumed = engine.consumption_for_meter(
            meter,
            dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
        )
    assert consumed == Decimal("0.000")


def test_a_quarantined_meter_is_excluded_from_billing(tenant_a):
    """TorqueTrust quarantines a device whose behaviour looks wrong. A
    quarantined meter's numbers must not reach an invoice."""
    with tenant_context(tenant_a["tenant"].id):
        before = engine.consumption_for_site(
            tenant_a["site"],
            dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
            Meter.SERVICE_STEAM,
        )
        tenant_a["meter"].status = Meter.STATUS_QUARANTINED
        tenant_a["meter"].save(update_fields=["status"])
        after = engine.consumption_for_site(
            tenant_a["site"],
            dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc),
            Meter.SERVICE_STEAM,
        )
    assert before > 0
    assert after == Decimal("0.000")


# --- end to end ------------------------------------------------------------


def test_issue_invoice_produces_a_consistent_invoice(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        invoice, created = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        lines = list(invoice.lines.all())
    assert created
    assert invoice.consumption_units == Decimal("7455.500")
    assert sum(line.amount_paise for line in lines) == invoice.total_paise
    assert invoice.number.startswith("TL-202610-")
    assert invoice.due_on == PERIOD_END + dt.timedelta(days=30)


def test_invoice_numbers_increment_within_a_tenant(tenant_a):
    with tenant_context(tenant_a["tenant"].id):
        from metering.models import Site

        second = Site.objects.create(
            tenant=tenant_a["tenant"], code="PLANT-02", name="Second plant"
        )
        from billing.models import Contract

        Contract.objects.create(
            tenant=tenant_a["tenant"],
            site=second,
            tariff=tenant_a["tariff"],
            reference="CT-ACME-STEEL-002",
            starts_on=dt.date(2025, 7, 1),
        )
        first_invoice, _ = engine.issue_invoice(
            tenant_a["tenant"].id, tenant_a["site"], PERIOD_START, PERIOD_END
        )
        second_invoice, _ = engine.issue_invoice(
            tenant_a["tenant"].id, second, PERIOD_START, PERIOD_END
        )
    assert first_invoice.number.endswith("-00001")
    assert second_invoice.number.endswith("-00002")


def test_two_tenants_can_hold_the_same_invoice_number(both):
    """Numbering is per tenant, so Acme's TL-202610-00001 and Borax's are
    different invoices and neither constraint nor sequence collides."""
    a, b = both
    for tenant in (a, b):
        run_billing_for_tenant_result = run_billing(tenant)
        assert run_billing_for_tenant_result

    with tenant_context(a["tenant"].id):
        a_numbers = set(
            Invoice.objects.filter(tenant_id=a["tenant"].id).values_list("number", flat=True)
        )
    with tenant_context(b["tenant"].id):
        b_numbers = set(
            Invoice.objects.filter(tenant_id=b["tenant"].id).values_list("number", flat=True)
        )
    assert a_numbers == b_numbers == {"TL-202610-00001"}


def run_billing(tenant_fixture):
    from billing.engine import run_billing_for_tenant

    issued, _, failed = run_billing_for_tenant(
        tenant_fixture["tenant"], PERIOD_START, PERIOD_END
    )
    assert not failed, failed
    return issued
