"""
The API layer, tested for isolation as well as behaviour.

These tests complement test_rls_boundary.py rather than replacing it: they
check that a real HTTP request carrying tenant A's token cannot see tenant B,
which is the thing a customer actually does. The database-level suite is what
proves the guarantee holds when the application is wrong.
"""

from __future__ import annotations

import pytest

from billing.engine import run_billing_for_tenant
from tenancy.db import tenant_context

from .conftest import PERIOD_END, PERIOD_START, token_for

pytestmark = pytest.mark.django_db


def auth(client, user):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token_for(user)}")
    return client


def test_token_auth_returns_own_tenant(api_client, tenant_a):
    response = auth(api_client, tenant_a["user"]).get("/api/v1/me")
    assert response.status_code == 200
    assert response.json()["tenant_name"] == "Acme Steel"


def test_anonymous_request_is_rejected(api_client, both):
    assert api_client.get("/api/v1/sites/").status_code in (401, 403)


def test_site_list_shows_only_own_sites(api_client, both):
    a, b = both
    response = auth(api_client, a["user"]).get("/api/v1/sites/")
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["results"]}
    assert str(a["site"].id) in ids
    assert str(b["site"].id) not in ids


def test_fetching_another_tenants_site_by_id_is_404(api_client, both):
    """404, not 403: tenant A should not learn that the id exists at all."""
    a, b = both
    response = auth(api_client, a["user"]).get(f"/api/v1/sites/{b['site'].id}/")
    assert response.status_code == 404


def test_fetching_another_tenants_invoice_by_id_is_404(api_client, both):
    a, b = both
    for tenant in (a, b):
        run_billing_for_tenant(tenant["tenant"], PERIOD_START, PERIOD_END)
    with tenant_context(b["tenant"].id):
        from billing.models import Invoice

        b_invoice = Invoice.objects.filter(tenant_id=b["tenant"].id).first()
    assert b_invoice is not None

    response = auth(api_client, a["user"]).get(f"/api/v1/invoices/{b_invoice.id}/")
    assert response.status_code == 404


def test_explicit_site_query_parameter_cannot_cross_tenants(api_client, both):
    """Passing another tenant's site id as a filter yields an empty list."""
    a, b = both
    response = auth(api_client, a["user"]).get(f"/api/v1/meters/?site={b['site'].id}")
    assert response.status_code == 200
    assert response.json()["results"] == []


def test_consumption_series_covers_only_own_meters(api_client, both):
    a, b = both
    a_response = auth(api_client, a["user"]).get("/api/v1/consumption?days=60")
    b_response = auth(api_client, b["user"]).get("/api/v1/consumption?days=60")
    assert a_response.status_code == b_response.status_code == 200

    a_total = sum(float(p["units"]) for p in a_response.json()["points"])
    b_total = sum(float(p["units"]) for p in b_response.json()["points"])
    assert a_total > 0 and b_total > 0
    # Both tenants have identical consumption by construction; what matters is
    # that neither series is the sum of the two.
    assert a_total == pytest.approx(b_total)
    assert a_total == pytest.approx(240.5 * 31)


def test_summary_counts_only_own_tenant(api_client, both):
    a, _ = both
    body = auth(api_client, a["user"]).get("/api/v1/summary").json()
    assert body["sites"] == 1
    assert body["meters"]["total"] == 1
    assert body["audit_chain"]["intact"] is True


def test_contract_terms_are_scoped(api_client, both):
    a, b = both
    rows = auth(api_client, a["user"]).get("/api/v1/contracts/").json()["results"]
    references = {row["reference"] for row in rows}
    assert references == {"CT-ACME-STEEL-001"}
    assert "CT-BORAX-CHEM-001" not in references
