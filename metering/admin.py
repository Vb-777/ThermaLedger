from django.contrib import admin

from tenancy.admin import TenantScopedAdmin

from .models import Meter, Reading, Site


@admin.register(Site)
class SiteAdmin(TenantScopedAdmin):
    list_display = ("code", "name", "commissioned_on")
    search_fields = ("code", "name")


@admin.register(Meter)
class MeterAdmin(TenantScopedAdmin):
    list_display = ("serial", "site", "service", "unit", "status")
    list_filter = ("service", "status")
    search_fields = ("serial",)


@admin.register(Reading)
class ReadingAdmin(TenantScopedAdmin):
    list_display = ("meter", "recorded_at", "cumulative", "quality")
    list_filter = ("quality",)
    date_hierarchy = "recorded_at"
