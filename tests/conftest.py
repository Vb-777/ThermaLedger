"""
Fixtures build two complete, overlapping tenants: same site codes, same meter
serials, same billing period. If isolation were done by filtering on anything
other than tenant_id, these two would collide and the tests would catch it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from billing.models import Contract, Tariff, TariffSlab
from metering.models import Meter, Reading, Site
from tenancy.db import tenant_context
from tenancy.models import Tenant, TenantUser

PERIOD_START = dt.date(2026, 10, 1)
PERIOD_END = dt.date(2026, 11, 1)


@pytest.fixture(autouse=True)
def _reset_tenant_binding(db):
    """Every test starts with no tenant bound, which means no rows visible."""
    from tenancy import db as tenancy_db

    tenancy_db.clear_tenant()
    yield
    tenancy_db.clear_tenant()


def _build_tenant(slug: str, name: str, base_cumulative: Decimal, username: str):
    tenant = Tenant.objects.create(slug=slug, name=name, billing_email=f"{slug}@example.com")
    user = User.objects.create_user(username=username, password="portal-password-1")

    with tenant_context(tenant.id):
        TenantUser.objects.create(tenant=tenant, user=user, display_name=name)

        site = Site.objects.create(
            tenant=tenant,
            code="PLANT-01",  # same code in both tenants, on purpose
            name=f"{name} Plant 1",
            commissioned_on=dt.date(2025, 6, 1),
        )
        meter = Meter.objects.create(
            tenant=tenant,
            site=site,
            serial="MTR-0001",  # same serial in both tenants, on purpose
            service=Meter.SERVICE_STEAM,
            unit="kWh",
        )
        tariff = Tariff.objects.create(
            tenant=tenant,
            name=f"{name} steam tariff",
            service=Meter.SERVICE_STEAM,
            fixed_charge_paise=250_000,
            minimum_offtake_units=Decimal("1000"),
            tax_percent_bp=1800,
            effective_from=dt.date(2025, 1, 1),
        )
        TariffSlab.objects.create(
            tenant=tenant, tariff=tariff, upto_units=Decimal("5000"), rate_paise_per_unit=820
        )
        TariffSlab.objects.create(
            tenant=tenant, tariff=tariff, upto_units=Decimal("20000"), rate_paise_per_unit=760
        )
        TariffSlab.objects.create(
            tenant=tenant, tariff=tariff, upto_units=None, rate_paise_per_unit=710
        )
        Contract.objects.create(
            tenant=tenant,
            site=site,
            tariff=tariff,
            reference=f"CT-{slug.upper()}-001",
            starts_on=dt.date(2025, 7, 1),
            payment_terms_days=30,
        )

        # Opening reading just before the period, then daily rises inside it.
        cumulative = base_cumulative
        Reading.objects.create(
            tenant=tenant,
            meter=meter,
            recorded_at=dt.datetime(2026, 9, 30, 23, 0, tzinfo=dt.timezone.utc),
            cumulative=cumulative,
        )
        for day in range(31):
            cumulative += Decimal("240.500")
            Reading.objects.create(
                tenant=tenant,
                meter=meter,
                recorded_at=dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.timezone.utc)
                + dt.timedelta(days=day),
                cumulative=cumulative,
            )

    return {
        "tenant": tenant,
        "user": user,
        "site": site,
        "meter": meter,
        "tariff": tariff,
    }


@pytest.fixture
def tenant_a(db):
    return _build_tenant("acme-steel", "Acme Steel", Decimal("10000.000"), "acme-portal")


@pytest.fixture
def tenant_b(db):
    return _build_tenant("borax-chem", "Borax Chemicals", Decimal("50000.000"), "borax-portal")


@pytest.fixture
def both(tenant_a, tenant_b):
    return tenant_a, tenant_b


@pytest.fixture
def api_client():
    from rest_framework.test import APIClient

    return APIClient()


def token_for(user) -> str:
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=user)
    return token.key
