"""
The application half of tenant isolation - the second layer, not the only one.

RLS below means a missed filter cannot leak. App scoping here means the common
path never *relies* on the database catching a mistake: the queryset is already
narrowed, the ORM emits the tenant predicate, and the query planner gets an
index-friendly filter rather than leaning on the policy expression.

The two layers are independent. `tests/test_rls_boundary.py` proves the lower
one holds with the upper one removed entirely - raw SQL, no ORM, no manager.
"""

from __future__ import annotations

from django.db import models

from . import db


class TenantScopedQuerySet(models.QuerySet):
    def for_tenant(self, tenant):
        tid = getattr(tenant, "id", tenant)
        return self.filter(tenant_id=tid)

    def for_current_tenant(self):
        """Narrow to whichever tenant the connection is bound to.

        If nothing is bound this returns `none()` rather than everything, which
        matches the database's fail-closed behaviour instead of contradicting it.
        """
        tid = db.current_tenant_id()
        if tid is None:
            return self.none()
        return self.filter(tenant_id=tid)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    use_in_migrations = False
