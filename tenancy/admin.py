from django.contrib import admin

from .models import Tenant, TenantUser


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "gstin", "is_active", "created_at")
    search_fields = ("name", "slug", "gstin")


@admin.register(TenantUser)
class TenantUserAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "display_name")
    list_filter = ("role", "tenant")


class TenantScopedAdmin(admin.ModelAdmin):
    """Admin base for RLS-protected models.

    Staff see only the tenant currently bound to the connection, which they
    choose with ?tenant=<uuid>. There is no "all tenants" view, because there is
    no query that could produce one - the policy would filter it to nothing.
    """

    def changelist_view(self, request, extra_context=None):
        chosen = request.GET.get("tenant")
        if chosen:
            request.session["admin_tenant_id"] = chosen
        extra_context = extra_context or {}
        extra_context["active_tenant"] = request.session.get("admin_tenant_id")
        return super().changelist_view(request, extra_context=extra_context)
