"""
Independent re-check of every issued invoice.

This does NOT call billing/engine.py. It recomputes each invoice from the raw
readings and the stored tariff with a second, deliberately plainer
implementation, then compares. An engine bug that is consistent with itself
would still be caught, because the oracle does not share the engine's code.

Prints the two numbers the README asks for:
  [INVOICE_ACCURACY]  -> invoices verified / invoices checked
  [AUDIT_CHAIN]       -> events verified per tenant
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.core.management.base import BaseCommand

from auditlog.models import AuditEvent
from billing.models import Invoice
from metering.models import Meter, Reading
from tenancy.db import tenant_context
from tenancy.models import Tenant


def oracle_consumption(meter, start, end) -> Decimal:
    values = list(
        Reading.objects.filter(meter=meter, recorded_at__lt=end)
        .order_by("recorded_at")
        .values_list("recorded_at", "cumulative")
    )
    inside = [v for ts, v in values if ts >= start]
    before = [v for ts, v in values if ts < start]
    series = ([before[-1]] + inside) if before else inside
    if len(series) < 2:
        return Decimal("0.000")
    total = Decimal("0")
    for i in range(1, len(series)):
        step = series[i] - series[i - 1]
        total += step if step >= 0 else series[i]
    return (total * meter.multiplier).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def oracle_total(invoice) -> tuple[Decimal, int]:
    """Recompute (billed_units, total_paise) from scratch."""
    import datetime as dt

    tariff = invoice.tariff
    start = dt.datetime.combine(invoice.period_start, dt.time.min, tzinfo=dt.timezone.utc)
    end = dt.datetime.combine(invoice.period_end, dt.time.min, tzinfo=dt.timezone.utc)

    consumption = Decimal("0")
    for meter in Meter.objects.filter(
        site=invoice.site, service=tariff.service, status=Meter.STATUS_ACTIVE
    ):
        consumption += oracle_consumption(meter, start, end)
    consumption = consumption.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

    billed = max(consumption, Decimal(tariff.minimum_offtake_units or 0))

    subtotal = Decimal("0")
    remaining = billed
    floor = Decimal("0")
    for slab in tariff.slabs_in_order():
        if remaining <= 0:
            break
        band = remaining if slab.upto_units is None else min(remaining, Decimal(slab.upto_units) - floor)
        if band <= 0:
            continue
        subtotal += (band.quantize(Decimal("0.001")) * Decimal(slab.rate_paise_per_unit)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        remaining -= band
        if slab.upto_units is not None:
            floor = Decimal(slab.upto_units)
    subtotal += Decimal(tariff.fixed_charge_paise)

    adjustments = sum(
        (a.signed_paise for a in invoice.applied_adjustments.all()), 0
    )
    taxable = int(subtotal) + adjustments
    tax = (Decimal(taxable) * Decimal(tariff.tax_percent_bp) / Decimal(10000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return billed.quantize(Decimal("0.001")), taxable + int(tax)


class Command(BaseCommand):
    help = "Recompute every invoice independently and report accuracy."

    def add_arguments(self, parser):
        parser.add_argument("--tenant", help="Limit to one tenant slug.")

    def handle(self, *args, **opts):
        tenants = Tenant.objects.all()
        if opts["tenant"]:
            tenants = tenants.filter(slug=opts["tenant"])

        checked = matched = 0
        failures = []
        chain_report = []

        for tenant in tenants.order_by("slug"):
            with tenant_context(tenant.id):
                invoices = (
                    Invoice.objects.filter(tenant_id=tenant.id)
                    .select_related("site", "tariff")
                    .prefetch_related("tariff__slabs", "applied_adjustments", "lines")
                )
                for invoice in invoices:
                    checked += 1
                    exp_units, exp_total = oracle_total(invoice)
                    line_sum = sum(line.amount_paise for line in invoice.lines.all())

                    problems = []
                    if invoice.billed_units != exp_units:
                        problems.append(f"billed_units {invoice.billed_units} != {exp_units}")
                    if invoice.total_paise != exp_total:
                        problems.append(f"total_paise {invoice.total_paise} != {exp_total}")
                    if line_sum != invoice.total_paise:
                        problems.append(f"lines sum to {line_sum}, total says {invoice.total_paise}")

                    if problems:
                        failures.append((tenant.slug, invoice.number, problems))
                    else:
                        matched += 1

                ok, events, bad = AuditEvent.verify_chain(tenant.id)
                chain_report.append((tenant.slug, ok, events, bad))

        for slug, number, problems in failures:
            self.stderr.write(self.style.ERROR(f"{slug} {number}: " + "; ".join(problems)))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("INVOICE ACCURACY"))
        self.stdout.write(
            f"  {matched}/{checked} invoices reproduced exactly by an independent recomputation"
        )
        self.stdout.write(
            f"  {checked - matched} discrepancies"
            "   <- record this line: README slot [INVOICE_ACCURACY]"
        )
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("AUDIT CHAIN"))
        for slug, ok, events, bad in chain_report:
            state = "intact" if ok else f"BROKEN at {bad}"
            self.stdout.write(f"  {slug}: {events} events, {state}")
        self.stdout.write("  ^ record these: README slot [AUDIT_CHAIN]")
