"""Bind each request's database transaction to the caller's tenant."""

from __future__ import annotations

import logging

from . import db

log = logging.getLogger(__name__)


class TenantBindingMiddleware:
    """Resolve request.user -> tenant and set the RLS variable for the request.

    Runs inside the ATOMIC_REQUESTS transaction, so the setting dies with it.
    An unauthenticated or unmapped request leaves the variable unset, which the
    policy reads as "no rows" - so a view that forgets a permission check still
    returns an empty result rather than another customer's data.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant_id = self._resolve(request)
        request.tenant_id = tenant_id
        db.set_tenant(tenant_id)
        try:
            return self.get_response(request)
        finally:
            db.clear_tenant()

    @classmethod
    def _resolve(cls, request):
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            # DRF authenticates inside the view, which is far too late - the
            # connection has to be bound before the first query runs. So the
            # token header is resolved here instead.
            user = cls._user_from_token(request)
        if user is None or not user.is_authenticated:
            return None
        tenant_user = getattr(user, "tenant_user", None)
        if tenant_user is None:
            # Staff with no tenant mapping (e.g. a Django superuser) may pick a
            # tenant explicitly. They still get exactly one tenant at a time.
            if user.is_staff:
                chosen = request.session.get("admin_tenant_id") or request.GET.get("tenant")
                return chosen or None
            log.warning("authenticated user %s has no tenant mapping", user.pk)
            return None
        return tenant_user.tenant_id

    @staticmethod
    def _user_from_token(request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        parts = header.split()
        if len(parts) != 2 or parts[0].lower() != "token":
            return None
        from rest_framework.authtoken.models import Token

        token = (
            Token.objects.select_related("user__tenant_user")
            .filter(key=parts[1])
            .first()
        )
        if token is None or not token.user.is_active:
            return None
        # Hand the resolved user to DRF's own authentication later in the stack;
        # it will re-verify the token independently.
        return token.user
