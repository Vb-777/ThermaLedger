"""
The database half of tenant isolation.

The contract is deliberately fail-closed:

    app.tenant_id unset  ->  current_setting('app.tenant_id', true) returns NULL
                         ->  `tenant_id = NULL` is NULL, never true
                         ->  zero rows, for SELECT, UPDATE and DELETE alike

There is no bypass flag and no "admin sees everything" escape hatch. Code that
legitimately spans tenants (the billing runner, the seeder, the admin site)
loops over tenants and enters each context in turn. That is slower to write and
much easier to defend: there is no code path that can accidentally be granted
cross-tenant visibility, because no such path exists.
"""

from __future__ import annotations

import contextlib
import uuid

from django.db import connection

TENANT_SETTING = "app.tenant_id"

# Tables carrying a tenant_id column that RLS is enforced on. Keep in sync with
# the models; `manage.py enable_rls --check` fails loudly if a model declares
# TenantOwnedModel but is missing here.
RLS_TABLES = (
    "metering_site",
    "metering_meter",
    "metering_reading",
    "billing_tariff",
    "billing_tariffslab",
    "billing_contract",
    "billing_adjustment",
    "billing_invoice",
    "billing_invoiceline",
    "billing_idempotencykey",
    "auditlog_auditevent",
)

# tenancy_tenantuser is deliberately NOT in the list above. It is the table that
# answers "which tenant is this request?", so it has to be readable before a
# tenant context exists - putting it under the policy would make the lookup
# return zero rows and no request could ever be bound. It is protected instead
# by being reachable only through request.user's own OneToOne row.


class RlsBypassError(RuntimeError):
    """Raised when the connected role would silently ignore RLS."""


def current_tenant_id() -> uuid.UUID | None:
    with connection.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", [TENANT_SETTING])
        raw = cur.fetchone()[0]
    if not raw:
        return None
    return uuid.UUID(raw)


def set_tenant(tenant_id) -> None:
    """Bind the current transaction to a tenant.

    is_local=true scopes the setting to the enclosing transaction, so it cannot
    survive onto the next request that reuses this pooled connection. This is
    why settings.ATOMIC_REQUESTS must stay on.
    """
    value = "" if tenant_id is None else str(tenant_id)
    with connection.cursor() as cur:
        cur.execute("SELECT set_config(%s, %s, true)", [TENANT_SETTING, value])


def clear_tenant() -> None:
    set_tenant(None)


@contextlib.contextmanager
def tenant_context(tenant_id):
    """Run a block bound to one tenant, restoring whatever was bound before.

    Accepts a Tenant instance or a UUID/str.
    """
    tid = getattr(tenant_id, "id", tenant_id)
    previous = current_tenant_id()
    set_tenant(tid)
    try:
        yield
    finally:
        set_tenant(previous)


def assert_role_cannot_bypass_rls() -> None:
    """Fail loudly if the connected role ignores row-level security.

    Postgres exempts superusers and BYPASSRLS roles from every policy. Running
    the application - or worse, the isolation test suite - as such a role means
    every policy is inert while every test still passes. This check is called
    from enable_rls and asserted again in the test suite.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover - current_user always exists
        raise RlsBypassError("could not resolve the current database role")
    is_super, can_bypass = row
    if is_super or can_bypass:
        with connection.cursor() as cur:
            cur.execute("SELECT current_user")
            role = cur.fetchone()[0]
        raise RlsBypassError(
            f"database role {role!r} is superuser={is_super} bypassrls={can_bypass}. "
            "Row-level security is silently ignored for such roles, so tenant "
            "isolation would not actually be enforced and the isolation tests "
            "would pass for the wrong reason. Connect as a dedicated "
            "non-superuser role (see docker/initdb/01-app-role.sql)."
        )


def enable_rls(verbose: bool = False) -> list[str]:
    """Idempotently enable and (re)create the RLS policy on every tenant table.

    FORCE ROW LEVEL SECURITY matters: without it the table *owner* is exempt,
    and the application role owns these tables because it ran the migrations.
    """
    assert_role_cannot_bypass_rls()
    applied = []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()"
        )
        existing = {r[0] for r in cur.fetchall()}

        for table in RLS_TABLES:
            if table not in existing:
                continue
            policy = f"{table}_tenant_isolation"
            cur.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
            cur.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
            cur.execute(f'DROP POLICY IF EXISTS "{policy}" ON "{table}"')
            cur.execute(
                f'CREATE POLICY "{policy}" ON "{table}" '
                f"FOR ALL "
                f"USING (tenant_id = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid) "
                f"WITH CHECK (tenant_id = NULLIF(current_setting('{TENANT_SETTING}', true), '')::uuid)"
            )
            applied.append(table)
            if verbose:
                print(f"  RLS enforced on {table}")
    return applied
