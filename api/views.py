"""
Read-only customer and operations API.

Every queryset here goes through `.for_current_tenant()`. That is the app-level
layer; the RLS policy underneath would reduce an unscoped queryset to nothing
anyway. Both are deliberate - see tenancy/managers.py.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from django.db.models import Count, F, Sum, Window
from django.db.models.functions import Lag, TruncDay, TruncHour
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from auditlog.models import AuditEvent
from billing.models import Contract, Invoice
from metering.models import Meter, Reading, Site

from .serializers import (
    ContractSerializer,
    InvoiceListSerializer,
    InvoiceSerializer,
    MeterSerializer,
    SiteSerializer,
)


class SiteViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SiteSerializer

    def get_queryset(self):
        return Site.objects.for_current_tenant().order_by("code")


class MeterViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MeterSerializer

    def get_queryset(self):
        qs = Meter.objects.for_current_tenant().select_related("site").order_by("serial")
        site = self.request.query_params.get("site")
        return qs.filter(site_id=site) if site else qs


class InvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    def get_serializer_class(self):
        return InvoiceSerializer if self.action == "retrieve" else InvoiceListSerializer

    def get_queryset(self):
        qs = Invoice.objects.for_current_tenant().select_related("site")
        if self.action == "retrieve":
            qs = qs.prefetch_related("lines")
        site = self.request.query_params.get("site")
        if site:
            qs = qs.filter(site_id=site)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs.order_by("-period_start", "number")


class ContractViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ContractSerializer

    def get_queryset(self):
        return (
            Contract.objects.for_current_tenant()
            .select_related("site", "tariff")
            .prefetch_related("tariff__slabs")
            .order_by("-starts_on")
        )


class ConsumptionView(APIView):
    """Time-series consumption for the portal chart.

    Readings are cumulative, so each bucket reports the rise across it rather
    than the register value, which is what a customer expects to see.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        granularity = request.query_params.get("granularity", "day")
        trunc = TruncHour if granularity == "hour" else TruncDay
        days = min(int(request.query_params.get("days", 30)), 365)
        since = timezone.now() - dt.timedelta(days=days)

        readings = Reading.objects.for_current_tenant().filter(recorded_at__gte=since)
        site = request.query_params.get("site")
        if site:
            readings = readings.filter(meter__site_id=site)

        # Per-reading delta against the previous reading of the same meter, via
        # a window function. Bucketing min/max instead would silently drop the
        # consumption that falls between the last reading of one bucket and the
        # first of the next.
        rows = (
            readings.annotate(bucket=trunc("recorded_at"))
            .annotate(
                previous=Window(
                    expression=Lag("cumulative"),
                    partition_by=[F("meter_id")],
                    order_by=F("recorded_at").asc(),
                )
            )
            .values("bucket", "cumulative", "previous")
        )

        series: dict[dt.datetime, Decimal] = {}
        for row in rows:
            if row["previous"] is None:
                continue
            delta = row["cumulative"] - row["previous"]
            if delta < 0:
                # Register reset: count up from zero, same rule the billing
                # engine applies.
                delta = row["cumulative"]
            series[row["bucket"]] = series.get(row["bucket"], Decimal("0")) + delta

        return Response(
            {
                "granularity": granularity,
                "days": days,
                "points": [
                    {"bucket": bucket.isoformat(), "units": str(value.quantize(Decimal("0.001")))}
                    for bucket, value in sorted(series.items())
                ],
            }
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def summary(request):
    """The single ops summary page - deliberately one endpoint, not a dashboard."""
    sites = Site.objects.for_current_tenant()
    meters = Meter.objects.for_current_tenant()
    invoices = Invoice.objects.for_current_tenant()
    readings = Reading.objects.for_current_tenant()

    last_24h = timezone.now() - dt.timedelta(hours=24)
    outstanding = invoices.filter(status=Invoice.STATUS_ISSUED).aggregate(
        n=Count("id"), total=Sum("total_paise")
    )
    chain_ok, chain_len, bad = AuditEvent.verify_chain(request.tenant_id)

    return Response(
        {
            "sites": sites.count(),
            "meters": {
                "total": meters.count(),
                "active": meters.filter(status=Meter.STATUS_ACTIVE).count(),
                "quarantined": meters.filter(status=Meter.STATUS_QUARANTINED).count(),
            },
            "readings_last_24h": readings.filter(recorded_at__gte=last_24h).count(),
            "latest_reading_at": (
                readings.order_by("-recorded_at").values_list("recorded_at", flat=True).first()
            ),
            "invoices": {
                "total": invoices.count(),
                "outstanding_count": outstanding["n"] or 0,
                "outstanding_rupees": round((outstanding["total"] or 0) / 100, 2),
            },
            "audit_chain": {"intact": chain_ok, "events": chain_len, "first_bad_event": bad},
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def me(request):
    tenant_user = getattr(request.user, "tenant_user", None)
    return Response(
        {
            "username": request.user.username,
            "tenant_id": str(request.tenant_id) if request.tenant_id else None,
            "tenant_name": tenant_user.tenant.name if tenant_user else None,
            "role": tenant_user.role if tenant_user else None,
            "display_name": tenant_user.display_name if tenant_user else "",
        }
    )
