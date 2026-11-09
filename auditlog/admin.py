from django.contrib import admin

from tenancy.admin import TenantScopedAdmin

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(TenantScopedAdmin):
    list_display = ("sequence", "action", "actor", "created_at", "hash")
    list_filter = ("action",)
    readonly_fields = tuple(f.name for f in AuditEvent._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
