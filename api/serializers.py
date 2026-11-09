from rest_framework import serializers

from billing.models import Contract, Invoice, InvoiceLine
from metering.models import Meter, Site


class SiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Site
        fields = ["id", "code", "name", "address", "latitude", "longitude", "commissioned_on"]


class MeterSerializer(serializers.ModelSerializer):
    site_code = serializers.CharField(source="site.code", read_only=True)

    class Meta:
        model = Meter
        fields = ["id", "serial", "site", "site_code", "service", "unit", "status", "installed_on"]


class InvoiceLineSerializer(serializers.ModelSerializer):
    amount_rupees = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceLine
        fields = [
            "position", "kind", "description", "units",
            "rate_paise_per_unit", "amount_paise", "amount_rupees",
        ]

    def get_amount_rupees(self, obj):
        return round(obj.amount_paise / 100, 2)


class InvoiceSerializer(serializers.ModelSerializer):
    site_code = serializers.CharField(source="site.code", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)
    total_rupees = serializers.SerializerMethodField()
    lines = InvoiceLineSerializer(many=True, read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "id", "number", "site", "site_code", "site_name",
            "period_start", "period_end", "consumption_units", "billed_units",
            "subtotal_paise", "adjustments_paise", "tax_paise", "total_paise",
            "total_rupees", "status", "issued_at", "due_on", "lines",
        ]

    def get_total_rupees(self, obj):
        return round(obj.total_paise / 100, 2)


class InvoiceListSerializer(InvoiceSerializer):
    """Billing history does not need every line of every invoice."""

    class Meta(InvoiceSerializer.Meta):
        fields = [f for f in InvoiceSerializer.Meta.fields if f != "lines"]


class ContractSerializer(serializers.ModelSerializer):
    site_code = serializers.CharField(source="site.code", read_only=True)
    tariff_name = serializers.CharField(source="tariff.name", read_only=True)
    service = serializers.CharField(source="tariff.service", read_only=True)
    minimum_offtake_units = serializers.DecimalField(
        source="tariff.minimum_offtake_units", max_digits=16, decimal_places=3, read_only=True
    )
    slabs = serializers.SerializerMethodField()

    class Meta:
        model = Contract
        fields = [
            "id", "reference", "site", "site_code", "tariff_name", "service",
            "starts_on", "ends_on", "payment_terms_days",
            "minimum_offtake_units", "slabs",
        ]

    def get_slabs(self, obj):
        return [
            {
                "upto_units": slab.upto_units,
                "rate_rupees_per_unit": round(slab.rate_paise_per_unit / 100, 4),
            }
            for slab in obj.tariff.slabs_in_order()
        ]


class ConsumptionPointSerializer(serializers.Serializer):
    bucket = serializers.DateTimeField()
    units = serializers.DecimalField(max_digits=16, decimal_places=3)
