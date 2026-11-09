from django.contrib import admin

from tenancy.admin import TenantScopedAdmin

from .models import Adjustment, Contract, IdempotencyKey, Invoice, InvoiceLine, Tariff, TariffSlab


class TariffSlabInline(admin.TabularInline):
    model = TariffSlab
    extra = 1


@admin.register(Tariff)
class TariffAdmin(TenantScopedAdmin):
    list_display = ("name", "service", "minimum_offtake_units", "fixed_charge_paise", "effective_from")
    inlines = [TariffSlabInline]


@admin.register(Contract)
class ContractAdmin(TenantScopedAdmin):
    list_display = ("reference", "site", "tariff", "starts_on", "ends_on", "payment_terms_days")


@admin.register(Adjustment)
class AdjustmentAdmin(TenantScopedAdmin):
    list_display = ("site", "kind", "amount_paise", "reason", "applied_to", "created_at")
    list_filter = ("kind",)


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0
    readonly_fields = ("position", "kind", "description", "units", "rate_paise_per_unit", "amount_paise")
    can_delete = False


@admin.register(Invoice)
class InvoiceAdmin(TenantScopedAdmin):
    list_display = ("number", "site", "period_start", "period_end", "billed_units", "total_paise", "status")
    list_filter = ("status",)
    search_fields = ("number",)
    inlines = [InvoiceLineInline]
    readonly_fields = (
        "number", "site", "tariff", "period_start", "period_end",
        "consumption_units", "billed_units", "subtotal_paise",
        "adjustments_paise", "tax_paise", "total_paise", "issued_at",
    )

    def has_add_permission(self, request):
        # Invoices come from the billing runner, never from a hand edit.
        return False


@admin.register(IdempotencyKey)
class IdempotencyKeyAdmin(TenantScopedAdmin):
    list_display = ("key", "invoice", "created_at")
    readonly_fields = ("key", "invoice", "created_at")
