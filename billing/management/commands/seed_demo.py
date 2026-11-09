"""
Realistic demo data: three industrial customers, eight sites, fourteen meters,
ninety days of hourly readings with plausible load shapes.

Two tenants deliberately share a site code and a meter serial, so the demo
itself would expose a tenant-isolation bug rather than hiding one.
"""

from __future__ import annotations

import datetime as dt
import math
import random
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from rest_framework.authtoken.models import Token

from billing.models import Adjustment, Contract, Tariff, TariffSlab
from metering.models import Meter, Reading, Site
from tenancy.db import tenant_context
from tenancy.models import Tenant, TenantUser

DEMO_PASSWORD = "thermaledger-demo"

TENANTS = [
    {
        "slug": "acme-steel",
        "name": "Acme Steel Rolling Mills",
        "gstin": "36AACCA1234F1Z5",
        "sites": [
            ("PLANT-01", "Patancheru Rolling Mill", 17.5311, 78.2644),
            ("PLANT-02", "Bollaram Annealing Line", 17.5449, 78.3215),
            ("PLANT-03", "Jeedimetla Finishing", 17.5062, 78.4451),
        ],
        "base_load": 420.0,
        "shift_pattern": "three-shift",
    },
    {
        "slug": "borax-chem",
        "name": "Borax Chemicals",
        "gstin": "36AABCB5678G1Z2",
        "sites": [
            ("PLANT-01", "Pashamylaram Reactor Hall", 17.5205, 78.2131),
            ("PLANT-02", "Gaddapotharam Dryer Block", 17.5588, 78.1902),
        ],
        "base_load": 260.0,
        "shift_pattern": "continuous",
    },
    {
        "slug": "veda-textiles",
        "name": "Veda Textiles",
        "gstin": "36AADCV9012H1Z8",
        "sites": [
            ("DYE-01", "Sangareddy Dye House", 17.6253, 78.0820),
            ("DYE-02", "Zaheerabad Processing", 17.6811, 77.6089),
            ("DYE-03", "Medak Finishing Unit", 18.0457, 78.2635),
        ],
        "base_load": 180.0,
        "shift_pattern": "day-shift",
    },
]

SHIFT_SHAPES = {
    # hour-of-day multiplier; what the chart in the portal will show
    "three-shift": lambda h: 0.85 + 0.25 * math.sin((h - 6) * math.pi / 12),
    "continuous": lambda h: 0.97 + 0.06 * math.sin(h * math.pi / 12),
    "day-shift": lambda h: 0.25 if h < 7 or h > 19 else 1.15,
}


class Command(BaseCommand):
    help = "Create demo tenants, sites, meters, 90 days of readings, tariffs and contracts."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=90)
        parser.add_argument("--interval-minutes", type=int, default=60)
        parser.add_argument("--seed", type=int, default=20261109)
        parser.add_argument(
            "--reset", action="store_true", help="Delete existing demo tenants first."
        )

    def handle(self, *args, **opts):
        random.seed(opts["seed"])
        if opts["reset"]:
            removed = Tenant.objects.filter(slug__in=[t["slug"] for t in TENANTS]).delete()
            self.stdout.write(f"removed previous demo data ({removed[0]} rows)")

        end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(days=opts["days"])
        step = dt.timedelta(minutes=opts["interval_minutes"])

        totals = {"sites": 0, "meters": 0, "readings": 0}
        for spec in TENANTS:
            counts = self._seed_tenant(spec, start, end, step)
            for key, value in counts.items():
                totals[key] += value
            self.stdout.write(
                f"{spec['slug']}: {counts['sites']} sites, {counts['meters']} meters, "
                f"{counts['readings']} readings"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSeeded {len(TENANTS)} tenants, {totals['sites']} sites, "
                f"{totals['meters']} meters, {totals['readings']} readings.\n"
                f"Portal logins (password: {DEMO_PASSWORD}):\n"
                + "\n".join(f"  {t['slug']}-portal   -> {t['name']}" for t in TENANTS)
                + "\n\nNext: python manage.py run_billing --month "
                f"{(end - dt.timedelta(days=35)):%Y-%m}"
            )
        )

    @transaction.atomic
    def _seed_tenant(self, spec, start, end, step):
        tenant = Tenant.objects.create(
            slug=spec["slug"],
            name=spec["name"],
            gstin=spec["gstin"],
            billing_email=f"accounts@{spec['slug']}.example.in",
        )
        username = f"{spec['slug']}-portal"
        user = User.objects.create_user(username=username, password=DEMO_PASSWORD)
        Token.objects.get_or_create(user=user)

        counts = {"sites": 0, "meters": 0, "readings": 0}
        shape = SHIFT_SHAPES[spec["shift_pattern"]]

        with tenant_context(tenant.id):
            TenantUser.objects.create(
                tenant=tenant, user=user, display_name=spec["name"], role=TenantUser.ROLE_CUSTOMER
            )

            steam_tariff = Tariff.objects.create(
                tenant=tenant,
                name=f"{spec['name']} - Steam as a Service",
                service=Meter.SERVICE_STEAM,
                fixed_charge_paise=485_000,
                minimum_offtake_units=Decimal("25000"),
                tax_percent_bp=1800,
                effective_from=dt.date(2025, 4, 1),
            )
            for cap, rate in ((50_000, 845), (150_000, 792), (None, 744)):
                TariffSlab.objects.create(
                    tenant=tenant,
                    tariff=steam_tariff,
                    upto_units=None if cap is None else Decimal(cap),
                    rate_paise_per_unit=rate,
                )

            thermal_tariff = Tariff.objects.create(
                tenant=tenant,
                name=f"{spec['name']} - Thermal Energy as a Service",
                service=Meter.SERVICE_THERMAL,
                fixed_charge_paise=312_000,
                minimum_offtake_units=Decimal("12000"),
                tax_percent_bp=1800,
                effective_from=dt.date(2025, 4, 1),
            )
            for cap, rate in ((40_000, 688), (None, 631)):
                TariffSlab.objects.create(
                    tenant=tenant,
                    tariff=thermal_tariff,
                    upto_units=None if cap is None else Decimal(cap),
                    rate_paise_per_unit=rate,
                )

            for index, (code, site_name, lat, lon) in enumerate(spec["sites"]):
                service = Meter.SERVICE_STEAM if index % 2 == 0 else Meter.SERVICE_THERMAL
                tariff = steam_tariff if service == Meter.SERVICE_STEAM else thermal_tariff
                site = Site.objects.create(
                    tenant=tenant,
                    code=code,
                    name=site_name,
                    address=f"{site_name}, Telangana",
                    latitude=Decimal(str(lat)),
                    longitude=Decimal(str(lon)),
                    commissioned_on=dt.date(2025, 5, 1) + dt.timedelta(days=30 * index),
                )
                counts["sites"] += 1
                Contract.objects.create(
                    tenant=tenant,
                    site=site,
                    tariff=tariff,
                    reference=f"CT-{spec['slug'].upper()[:4]}-{index + 1:03d}",
                    starts_on=dt.date(2025, 6, 1),
                    payment_terms_days=30,
                )

                meters = 2 if index == 0 else 1
                for meter_index in range(meters):
                    meter = Meter.objects.create(
                        tenant=tenant,
                        site=site,
                        serial=f"MTR-{index + 1:02d}{meter_index + 1:02d}",
                        service=service,
                        unit="kWh" if service == Meter.SERVICE_THERMAL else "kg",
                        installed_on=site.commissioned_on,
                    )
                    counts["meters"] += 1
                    counts["readings"] += self._seed_readings(
                        tenant, meter, start, end, step, spec["base_load"], shape
                    )

            # One pending credit, so at least one demo invoice carries an
            # adjustment line and the portal shows what that looks like.
            first_site = Site.objects.filter(tenant_id=tenant.id).order_by("code").first()
            Adjustment.objects.create(
                tenant=tenant,
                site=first_site,
                kind=Adjustment.KIND_CREDIT,
                amount_paise=47_500,
                reason="Credit for meter MTR-0101 downtime, 14-16 Sep",
            )

        return counts

    def _seed_readings(self, tenant, meter, start, end, step, base_load, shape):
        cumulative = Decimal(random.randint(80_000, 400_000))
        rows = []
        moment = start
        while moment < end:
            factor = shape(moment.hour)
            weekday_factor = 0.55 if moment.weekday() == 6 else 1.0
            noise = random.uniform(0.92, 1.08)
            rise = Decimal(str(round(base_load * factor * weekday_factor * noise, 3)))
            cumulative += rise
            rows.append(
                Reading(
                    tenant=tenant,
                    meter=meter,
                    recorded_at=moment,
                    cumulative=cumulative,
                    quality=(
                        Reading.QUALITY_ESTIMATED if random.random() < 0.004 else Reading.QUALITY_GOOD
                    ),
                )
            )
            moment += step
        Reading.objects.bulk_create(rows, batch_size=2000)
        return len(rows)
