from __future__ import annotations

import uuid

from django.db import models

from metering.models import Meter, Site
from tenancy.models import TenantOwnedModel

# Money is stored as integer paise throughout. Float rupees in a billing system
# is how you end up with invoices that disagree with themselves by a paisa and
# a customer who stops trusting the portal.


class Tariff(TenantOwnedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=120)
    service = models.CharField(max_length=16, choices=Meter.SERVICE_CHOICES)
    currency = models.CharField(max_length=3, default="INR")
    fixed_charge_paise = models.BigIntegerField(
        default=0, help_text="Standing charge applied once per billing period."
    )
    minimum_offtake_units = models.DecimalField(
        max_digits=16,
        decimal_places=3,
        default=0,
        help_text="Contracted floor. Consumption below this is billed at this level.",
    )
    tax_percent_bp = models.IntegerField(
        default=1800, help_text="Tax rate in basis points. 1800 = 18%."
    )
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-effective_from"]

    def __str__(self):
        return f"{self.name} ({self.service})"

    def slabs_in_order(self):
        return list(self.slabs.order_by(models.F("upto_units").asc(nulls_last=True)))


class TariffSlab(TenantOwnedModel):
    """A consumption band. `upto_units = NULL` is the open-ended final band."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tariff = models.ForeignKey(Tariff, on_delete=models.CASCADE, related_name="slabs")
    upto_units = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True)
    rate_paise_per_unit = models.BigIntegerField()

    class Meta:
        ordering = [models.F("upto_units").asc(nulls_last=True)]

    def __str__(self):
        cap = "and above" if self.upto_units is None else f"up to {self.upto_units}"
        return f"{cap} @ {self.rate_paise_per_unit}p"


class Contract(TenantOwnedModel):
    """What the customer portal shows under 'contract terms'."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="contracts")
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT, related_name="contracts")
    reference = models.CharField(max_length=64)
    starts_on = models.DateField()
    ends_on = models.DateField(null=True, blank=True)
    payment_terms_days = models.IntegerField(default=30)

    class Meta:
        ordering = ["-starts_on"]

    def __str__(self):
        return self.reference


class Adjustment(TenantOwnedModel):
    """A credit or debit applied to the next invoice for a site."""

    KIND_CREDIT = "credit"
    KIND_DEBIT = "debit"
    KIND_CHOICES = [(KIND_CREDIT, "Credit"), (KIND_DEBIT, "Debit")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="adjustments")
    kind = models.CharField(max_length=8, choices=KIND_CHOICES)
    amount_paise = models.BigIntegerField()
    reason = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_to = models.ForeignKey(
        "billing.Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="applied_adjustments",
    )

    class Meta:
        ordering = ["created_at"]

    @property
    def signed_paise(self) -> int:
        return -self.amount_paise if self.kind == self.KIND_CREDIT else self.amount_paise


class Invoice(TenantOwnedModel):
    STATUS_ISSUED = "issued"
    STATUS_PAID = "paid"
    STATUS_VOID = "void"
    STATUS_CHOICES = [
        (STATUS_ISSUED, "Issued"),
        (STATUS_PAID, "Paid"),
        (STATUS_VOID, "Void"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    number = models.CharField(max_length=32)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="invoices")
    tariff = models.ForeignKey(Tariff, on_delete=models.PROTECT, related_name="invoices")
    period_start = models.DateField()
    period_end = models.DateField()
    consumption_units = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    billed_units = models.DecimalField(
        max_digits=16,
        decimal_places=3,
        default=0,
        help_text="Consumption after the minimum-offtake floor is applied.",
    )
    subtotal_paise = models.BigIntegerField(default=0)
    adjustments_paise = models.BigIntegerField(default=0)
    tax_paise = models.BigIntegerField(default=0)
    total_paise = models.BigIntegerField(default=0)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ISSUED)
    issued_at = models.DateTimeField(auto_now_add=True)
    due_on = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "number"], name="uniq_invoice_number_per_tenant"
            ),
            # One invoice per site per period, enforced by the database rather
            # than by the runner remembering to check.
            models.UniqueConstraint(
                fields=["tenant", "site", "period_start", "period_end"],
                condition=models.Q(status__in=["issued", "paid"]),
                name="uniq_live_invoice_per_site_period",
            ),
        ]
        ordering = ["-period_start", "number"]

    def __str__(self):
        return self.number

    @property
    def total_rupees(self):
        return self.total_paise / 100


class InvoiceLine(TenantOwnedModel):
    KIND_SLAB = "slab"
    KIND_FIXED = "fixed"
    KIND_MINIMUM = "minimum"
    KIND_ADJUSTMENT = "adjustment"
    KIND_TAX = "tax"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    position = models.IntegerField()
    kind = models.CharField(max_length=16)
    description = models.CharField(max_length=300)
    units = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    rate_paise_per_unit = models.BigIntegerField(default=0)
    amount_paise = models.BigIntegerField(default=0)

    class Meta:
        ordering = ["position"]


class IdempotencyKey(TenantOwnedModel):
    """Paylink's charge-once guarantee, applied to invoice issuance.

    The unique constraint is the whole mechanism: a second billing run for the
    same site and period loses the insert race and is handed the invoice the
    first run produced, instead of issuing a second bill for the same kWh.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=200)
    invoice = models.ForeignKey(
        Invoice, null=True, blank=True, on_delete=models.CASCADE, related_name="idempotency_keys"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "key"], name="uniq_idempotency_key_per_tenant")
        ]

    def __str__(self):
        return self.key
