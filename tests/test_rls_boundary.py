"""
The isolation suite.

These tests deliberately bypass the ORM, the managers and the views. They open
a cursor, bind the connection to tenant A, and issue raw SQL naming tenant B's
primary keys directly. If the database returns a row, isolation has failed -
regardless of how correct the application code above it is.

A test asserting that an endpoint returns 403 would pass even with RLS switched
off entirely, because the view's own permission check would carry it. Nothing
here can pass that way: there is no view in the call stack.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection, transaction
from django.db.utils import InternalError, ProgrammingError

from auditlog.models import AuditEvent
from billing.models import Invoice
from metering.models import Meter, Reading, Site
from tenancy import db as tenancy_db
from tenancy.db import tenant_context

pytestmark = [pytest.mark.django_db, pytest.mark.isolation]


def rows(sql, params=None):
    with connection.cursor() as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()


# --------------------------------------------------------------------------
# 0. The precondition: if this fails, every other test in the file is vacuous.
# --------------------------------------------------------------------------


def test_connected_role_cannot_bypass_rls():
    """Superusers and BYPASSRLS roles ignore every policy silently.

    Running the suite as such a role would make all the tests below pass while
    enforcing nothing. This asserts the precondition rather than assuming it.
    """
    tenancy_db.assert_role_cannot_bypass_rls()


def test_every_tenant_table_has_a_forced_policy():
    for table in tenancy_db.RLS_TABLES:
        result = rows(
            "SELECT c.relrowsecurity, c.relforcerowsecurity, "
            "(SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
            "FROM pg_class c WHERE c.relname = %s",
            [table],
        )
        assert result, f"{table} does not exist"
        enabled, forced, policies = result[0]
        assert enabled, f"{table}: row-level security is not enabled"
        assert forced, (
            f"{table}: FORCE ROW LEVEL SECURITY is off, so the table owner - "
            "which is the application role, because it ran the migrations - is exempt"
        )
        assert policies >= 1, f"{table}: no policy attached"


# --------------------------------------------------------------------------
# 1. Cross-tenant reads, at the database level
# --------------------------------------------------------------------------


def test_select_by_primary_key_across_tenants_returns_no_rows(both):
    """The sharpest form: tenant A asks for tenant B's row by its exact id."""
    a, b = both
    targets = {
        "metering_site": b["site"].id,
        "metering_meter": b["meter"].id,
    }
    with tenant_context(a["tenant"].id):
        for table, pk in targets.items():
            result = rows(f'SELECT * FROM "{table}" WHERE id = %s', [str(pk)])
            assert result == [], (
                f"tenant A read {table} row {pk}, which belongs to tenant B"
            )


def test_select_readings_across_tenants_returns_no_rows(both):
    a, b = both
    with tenant_context(b["tenant"].id):
        b_reading_ids = list(
            Reading.objects.filter(tenant_id=b["tenant"].id).values_list("id", flat=True)
        )
    assert b_reading_ids, "fixture produced no readings for tenant B"

    with tenant_context(a["tenant"].id):
        result = rows(
            "SELECT id FROM metering_reading WHERE id = ANY(%s)", [b_reading_ids]
        )
        assert result == [], "tenant A read tenant B's meter readings"


def test_unqualified_select_returns_only_own_rows(both):
    """`SELECT *` with no WHERE clause - the query a forgotten filter produces."""
    a, b = both
    with tenant_context(a["tenant"].id):
        site_tenants = {r[0] for r in rows("SELECT DISTINCT tenant_id FROM metering_site")}
        reading_tenants = {r[0] for r in rows("SELECT DISTINCT tenant_id FROM metering_reading")}
    assert site_tenants == {a["tenant"].id}
    assert reading_tenants == {a["tenant"].id}
    assert b["tenant"].id not in site_tenants


def test_count_star_excludes_other_tenants(both):
    a, _ = both
    with tenant_context(a["tenant"].id):
        total = rows("SELECT count(*) FROM metering_reading")[0][0]
        own = rows(
            "SELECT count(*) FROM metering_reading WHERE tenant_id = %s",
            [str(a["tenant"].id)],
        )[0][0]
    assert total == own, "an aggregate saw rows the row-level read could not"


def test_join_cannot_be_used_to_reach_another_tenant(both):
    """Joining from an own-tenant row to a foreign row yields nothing.

    Policies apply per table, so the foreign side of the join is filtered too -
    a join is not a way around the boundary.
    """
    a, b = both
    with tenant_context(a["tenant"].id):
        result = rows(
            "SELECT r.id FROM metering_reading r "
            "JOIN metering_meter m ON m.id = r.meter_id "
            "WHERE m.id = %s",
            [str(b["meter"].id)],
        )
    assert result == []


def test_subquery_cannot_leak_another_tenants_values(both):
    a, b = both
    with tenant_context(a["tenant"].id):
        result = rows(
            "SELECT (SELECT max(cumulative) FROM metering_reading WHERE meter_id = %s)",
            [str(b["meter"].id)],
        )
    assert result[0][0] is None, "a scalar subquery read across the tenant boundary"


# --------------------------------------------------------------------------
# 2. Writes across the boundary
# --------------------------------------------------------------------------


def test_update_cannot_touch_another_tenants_rows(both):
    a, b = both
    with tenant_context(a["tenant"].id):
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE metering_site SET name = %s WHERE id = %s",
                ["hijacked", str(b["site"].id)],
            )
            assert cur.rowcount == 0, "tenant A updated tenant B's site"

    with tenant_context(b["tenant"].id):
        b["site"].refresh_from_db()
        assert b["site"].name != "hijacked"


def test_delete_cannot_touch_another_tenants_rows(both):
    a, b = both
    with tenant_context(b["tenant"].id):
        before = Reading.objects.filter(tenant_id=b["tenant"].id).count()

    with tenant_context(a["tenant"].id):
        with connection.cursor() as cur:
            cur.execute("DELETE FROM metering_reading")
            deleted_everything_visible = cur.rowcount

    with tenant_context(b["tenant"].id):
        after = Reading.objects.filter(tenant_id=b["tenant"].id).count()

    assert after == before, (
        f"an unqualified DELETE as tenant A removed {before - after} of tenant B's readings "
        f"(it deleted {deleted_everything_visible} rows in total)"
    )


def test_insert_tagged_with_another_tenant_is_rejected(both):
    """WITH CHECK stops a row being written *into* another tenant.

    Without it, isolation would only be read-side and tenant A could plant rows
    in tenant B's data.
    """
    a, b = both
    with tenant_context(a["tenant"].id):
        with pytest.raises((InternalError, ProgrammingError)):
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "INSERT INTO metering_site "
                        "(id, tenant_id, code, name, address) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        [str(uuid.uuid4()), str(b["tenant"].id), "SNEAK-01", "planted", ""],
                    )


def test_update_cannot_move_a_row_to_another_tenant(both):
    a, b = both
    with tenant_context(a["tenant"].id):
        with pytest.raises((InternalError, ProgrammingError)):
            with transaction.atomic():
                with connection.cursor() as cur:
                    cur.execute(
                        "UPDATE metering_site SET tenant_id = %s WHERE id = %s",
                        [str(b["tenant"].id), str(a["site"].id)],
                    )


# --------------------------------------------------------------------------
# 3. Fail-closed when no tenant is bound
# --------------------------------------------------------------------------


def test_unbound_connection_sees_nothing(both):
    """No tenant bound is treated as "no rows", not as "all rows".

    This is the behaviour that makes an unauthenticated request safe even if a
    view forgets its permission class.
    """
    tenancy_db.clear_tenant()
    for table in ("metering_site", "metering_meter", "metering_reading", "billing_invoice"):
        assert rows(f'SELECT count(*) FROM "{table}"')[0][0] == 0, (
            f"{table} was readable with no tenant bound"
        )


def test_unbound_connection_cannot_insert(both):
    tenancy_db.clear_tenant()
    with pytest.raises((InternalError, ProgrammingError)):
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute(
                    "INSERT INTO metering_site (id, tenant_id, code, name, address) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [str(uuid.uuid4()), str(uuid.uuid4()), "NOBODY-01", "orphan", ""],
                )


def test_garbage_tenant_setting_yields_no_rows(both):
    """A malformed setting must not error open into unfiltered access."""
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('app.tenant_id', '', true)")
    assert rows("SELECT count(*) FROM metering_site")[0][0] == 0


# --------------------------------------------------------------------------
# 4. The context manager itself
# --------------------------------------------------------------------------


def test_tenant_context_restores_the_previous_binding(both):
    a, b = both
    with tenant_context(a["tenant"].id):
        assert tenancy_db.current_tenant_id() == a["tenant"].id
        with tenant_context(b["tenant"].id):
            assert tenancy_db.current_tenant_id() == b["tenant"].id
        assert tenancy_db.current_tenant_id() == a["tenant"].id, (
            "a nested context leaked tenant B's binding back to the caller"
        )
    assert tenancy_db.current_tenant_id() is None


def test_tenant_context_restores_binding_after_an_exception(both):
    a, _ = both
    with tenant_context(a["tenant"].id):
        try:
            with tenant_context(uuid.uuid4()):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert tenancy_db.current_tenant_id() == a["tenant"].id


# --------------------------------------------------------------------------
# 5. The ORM layer agrees with the database layer
# --------------------------------------------------------------------------


def test_scoped_manager_returns_nothing_when_unbound(both):
    tenancy_db.clear_tenant()
    assert Site.objects.for_current_tenant().count() == 0
    assert Invoice.objects.for_current_tenant().count() == 0


def test_orm_filtered_to_another_tenant_still_returns_nothing(both):
    """Even an explicitly wrong ORM filter cannot cross the boundary."""
    a, b = both
    with tenant_context(a["tenant"].id):
        assert Site.objects.filter(tenant_id=b["tenant"].id).count() == 0
        assert Meter.objects.filter(tenant_id=b["tenant"].id).count() == 0
        assert AuditEvent.objects.filter(tenant_id=b["tenant"].id).count() == 0
