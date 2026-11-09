"""
The billing engine.

Three rules, in this order:

  1. Consumption for a period is the difference between the last reading at or
     before the period end and the last reading at or before the period start.
     Readings are cumulative, so a dropped packet costs resolution, not kWh.
  2. Billed units = max(consumption, contracted minimum offtake).
  3. Billed units are cut into slab bands, each band priced at its own rate,
     then fixed charge, then adjustments, then tax on the lot.

Every amount is integer paise and every division rounds once, at the end of the
line it belongs to, with ROUND_HALF_UP. Rounding per-line rather than once at
the bottom means the printed lines always add to the printed total - the
property `verify_billing.py` checks across every invoice.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Max

from auditlog.models import AuditEvent
from metering.models import Meter, Reading
from tenancy.db import tenant_context

from .models import Adjustment, Contract, IdempotencyKey, Invoice, InvoiceLine, Tariff

ZERO = Decimal("0")
UNIT_QUANTUM = Decimal("0.001")


class BillingError(RuntimeError):
    pass


def paise(value: Decimal) -> int:
    """Round a paise-valued Decimal to a whole paisa, half up."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def units(value: Decimal) -> Decimal:
    return value.quantize(UNIT_QUANTUM, rounding=ROUND_HALF_UP)


@dataclasses.dataclass
class ComputedLine:
    kind: str
    description: str
    units: Decimal
    rate_paise_per_unit: int
    amount_paise: int


@dataclasses.dataclass
class ComputedInvoice:
    consumption_units: Decimal
    billed_units: Decimal
    lines: list[ComputedLine]
    subtotal_paise: int
    adjustments_paise: int
    tax_paise: int
    total_paise: int


def consumption_for_meter(meter: Meter, start: dt.datetime, end: dt.datetime) -> Decimal:
    """Consumption over [start, end), handling meter resets.

    A reading lower than its predecessor means the register rolled over or the
    meter was replaced. Treating the drop as negative consumption would credit
    the customer for a hardware event, so each descending step is instead
    treated as a reset and the new reading counted from zero.
    """
    readings = list(
        Reading.objects.filter(
            meter=meter, recorded_at__gte=start, recorded_at__lt=end
        ).order_by("recorded_at").values_list("cumulative", flat=True)
    )

    opening = (
        Reading.objects.filter(meter=meter, recorded_at__lt=start)
        .order_by("-recorded_at")
        .values_list("cumulative", flat=True)
        .first()
    )

    if opening is None:
        if len(readings) < 2:
            return ZERO
        series = readings
    else:
        series = [opening, *readings]

    total = ZERO
    for previous, current in zip(series, series[1:]):
        if current >= previous:
            total += current - previous
        else:
            total += current  # register reset; count up from zero
    return units(total * meter.multiplier)


def consumption_for_site(site, start: dt.datetime, end: dt.datetime, service: str) -> Decimal:
    total = ZERO
    for meter in Meter.objects.filter(site=site, service=service, status=Meter.STATUS_ACTIVE):
        total += consumption_for_meter(meter, start, end)
    return units(total)


def price_slabs(tariff: Tariff, billable: Decimal) -> list[ComputedLine]:
    """Cut `billable` into the tariff's bands and price each band."""
    lines: list[ComputedLine] = []
    remaining = billable
    floor = ZERO
    for slab in tariff.slabs_in_order():
        if remaining <= ZERO:
            break
        cap = slab.upto_units
        band = remaining if cap is None else min(remaining, Decimal(cap) - floor)
        if band <= ZERO:
            continue
        amount = paise(units(band) * Decimal(slab.rate_paise_per_unit))
        label = (
            f"{floor:g}+ units"
            if cap is None
            else f"{floor:g}-{Decimal(cap):g} units"
        )
        lines.append(
            ComputedLine(
                kind=InvoiceLine.KIND_SLAB,
                description=f"Consumption {label} @ {slab.rate_paise_per_unit / 100:.2f}/unit",
                units=units(band),
                rate_paise_per_unit=slab.rate_paise_per_unit,
                amount_paise=amount,
            )
        )
        remaining -= band
        floor = Decimal(cap) if cap is not None else floor

    if remaining > ZERO:
        raise BillingError(
            f"tariff {tariff.name!r} has no open-ended final slab; "
            f"{remaining} units could not be priced"
        )
    return lines


def compute(
    tariff: Tariff,
    consumption: Decimal,
    adjustments: list[Adjustment],
) -> ComputedInvoice:
    minimum = Decimal(tariff.minimum_offtake_units or 0)
    billed = max(consumption, minimum)

    lines = price_slabs(tariff, billed)

    if billed > consumption:
        lines.append(
            ComputedLine(
                kind=InvoiceLine.KIND_MINIMUM,
                description=(
                    f"Minimum offtake applied: metered {consumption:g}, "
                    f"contracted floor {minimum:g}"
                ),
                units=units(billed - consumption),
                rate_paise_per_unit=0,
                amount_paise=0,
            )
        )

    if tariff.fixed_charge_paise:
        lines.append(
            ComputedLine(
                kind=InvoiceLine.KIND_FIXED,
                description="Fixed standing charge",
                units=Decimal("1"),
                rate_paise_per_unit=tariff.fixed_charge_paise,
                amount_paise=tariff.fixed_charge_paise,
            )
        )

    subtotal = sum(line.amount_paise for line in lines)

    adjustments_total = 0
    for adjustment in adjustments:
        lines.append(
            ComputedLine(
                kind=InvoiceLine.KIND_ADJUSTMENT,
                description=f"{adjustment.get_kind_display()}: {adjustment.reason}",
                units=ZERO,
                rate_paise_per_unit=0,
                amount_paise=adjustment.signed_paise,
            )
        )
        adjustments_total += adjustment.signed_paise

    taxable = subtotal + adjustments_total
    tax = paise(Decimal(taxable) * Decimal(tariff.tax_percent_bp) / Decimal(10000))
    if tax:
        lines.append(
            ComputedLine(
                kind=InvoiceLine.KIND_TAX,
                description=f"Tax @ {tariff.tax_percent_bp / 100:.2f}%",
                units=ZERO,
                rate_paise_per_unit=0,
                amount_paise=tax,
            )
        )

    return ComputedInvoice(
        consumption_units=units(consumption),
        billed_units=units(billed),
        lines=lines,
        subtotal_paise=subtotal,
        adjustments_paise=adjustments_total,
        tax_paise=tax,
        total_paise=taxable + tax,
    )


def idempotency_key(site_id, period_start: dt.date, period_end: dt.date) -> str:
    return f"invoice:{site_id}:{period_start.isoformat()}:{period_end.isoformat()}"


def next_invoice_number(tenant_id, period_start: dt.date) -> str:
    prefix = f"TL-{period_start:%Y%m}"
    last = (
        Invoice.objects.filter(tenant_id=tenant_id, number__startswith=prefix)
        .aggregate(m=Max("number"))
        .get("m")
    )
    serial = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}-{serial:05d}"


@transaction.atomic
def issue_invoice(
    tenant_id,
    site,
    period_start: dt.date,
    period_end: dt.date,
    actor: str = "billing-runner",
) -> tuple[Invoice, bool]:
    """Issue one invoice. Returns (invoice, created).

    Must be called inside the tenant's context. Replaying the same period
    returns the existing invoice with created=False and writes a
    `invoice.replayed` audit event - the run is visible without being
    double-charged.
    """
    key = idempotency_key(site.id, period_start, period_end)

    existing = (
        IdempotencyKey.objects.select_related("invoice").filter(tenant_id=tenant_id, key=key).first()
    )
    if existing and existing.invoice_id:
        AuditEvent.record(
            tenant_id,
            AuditEvent.ACTION_INVOICE_REPLAYED,
            {"invoice": str(existing.invoice_id), "key": key},
            actor=actor,
        )
        return existing.invoice, False

    contract = (
        Contract.objects.select_related("tariff")
        .filter(site=site, starts_on__lte=period_start)
        .exclude(ends_on__lt=period_end)
        .order_by("-starts_on")
        .first()
    )
    if contract is None:
        raise BillingError(f"site {site.code} has no contract covering {period_start}..{period_end}")
    tariff = contract.tariff

    start_dt = dt.datetime.combine(period_start, dt.time.min, tzinfo=dt.timezone.utc)
    end_dt = dt.datetime.combine(period_end, dt.time.min, tzinfo=dt.timezone.utc)
    consumption = consumption_for_site(site, start_dt, end_dt, tariff.service)

    pending = list(
        Adjustment.objects.filter(site=site, applied_to__isnull=True).order_by("created_at")
    )
    computed = compute(tariff, consumption, pending)

    try:
        with transaction.atomic():
            marker = IdempotencyKey.objects.create(tenant_id=tenant_id, key=key)
    except IntegrityError:
        # Another runner got there first between our check and our insert.
        existing = (
            IdempotencyKey.objects.select_related("invoice")
            .filter(tenant_id=tenant_id, key=key)
            .first()
        )
        if existing and existing.invoice_id:
            return existing.invoice, False
        raise BillingError(f"idempotency key {key} is held but carries no invoice")

    invoice = Invoice.objects.create(
        tenant_id=tenant_id,
        number=next_invoice_number(tenant_id, period_start),
        site=site,
        tariff=tariff,
        period_start=period_start,
        period_end=period_end,
        consumption_units=computed.consumption_units,
        billed_units=computed.billed_units,
        subtotal_paise=computed.subtotal_paise,
        adjustments_paise=computed.adjustments_paise,
        tax_paise=computed.tax_paise,
        total_paise=computed.total_paise,
        due_on=period_end + dt.timedelta(days=contract.payment_terms_days),
    )
    InvoiceLine.objects.bulk_create(
        [
            InvoiceLine(
                tenant_id=tenant_id,
                invoice=invoice,
                position=index,
                kind=line.kind,
                description=line.description,
                units=line.units,
                rate_paise_per_unit=line.rate_paise_per_unit,
                amount_paise=line.amount_paise,
            )
            for index, line in enumerate(computed.lines, start=1)
        ]
    )
    for adjustment in pending:
        adjustment.applied_to = invoice
        adjustment.save(update_fields=["applied_to"])

    marker.invoice = invoice
    marker.save(update_fields=["invoice"])

    AuditEvent.record(
        tenant_id,
        AuditEvent.ACTION_INVOICE_ISSUED,
        {
            "invoice": str(invoice.id),
            "number": invoice.number,
            "site": site.code,
            "period": [period_start.isoformat(), period_end.isoformat()],
            "consumption_units": str(computed.consumption_units),
            "billed_units": str(computed.billed_units),
            "subtotal_paise": computed.subtotal_paise,
            "adjustments_paise": computed.adjustments_paise,
            "tax_paise": computed.tax_paise,
            "total_paise": computed.total_paise,
            "tariff": tariff.name,
        },
        actor=actor,
    )
    return invoice, True


def run_billing_for_tenant(tenant, period_start: dt.date, period_end: dt.date, actor="billing-runner"):
    """Issue invoices for every site of one tenant, inside its own context."""
    from metering.models import Site

    issued, replayed, failed = [], [], []
    with tenant_context(tenant.id):
        for site in Site.objects.filter(tenant_id=tenant.id).order_by("code"):
            try:
                invoice, created = issue_invoice(
                    tenant.id, site, period_start, period_end, actor=actor
                )
            except BillingError as exc:
                failed.append((site.code, str(exc)))
                continue
            (issued if created else replayed).append(invoice)
    return issued, replayed, failed
