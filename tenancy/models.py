from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models

from .managers import TenantScopedManager


class Tenant(models.Model):
    """A customer of Clear Energy. Not itself RLS-protected: the row is only
    reachable by primary key from an authenticated user's own TenantUser."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    gstin = models.CharField(max_length=15, blank=True)
    billing_email = models.EmailField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TenantOwnedModel(models.Model):
    """Base for every row the RLS policy applies to.

    Carrying tenant_id on every table (rather than joining up to a parent) is
    what lets the policy be a single column comparison, which in turn is what
    makes it cheap enough to leave on for every query.
    """

    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="+")

    objects = TenantScopedManager()

    class Meta:
        abstract = True


class TenantUser(TenantOwnedModel):
    """Links a Django user to the one tenant whose data they may see."""

    ROLE_CUSTOMER = "customer"
    ROLE_OPS = "ops"
    ROLE_CHOICES = [(ROLE_CUSTOMER, "Customer"), (ROLE_OPS, "Operations")]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tenant_user"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_CUSTOMER)
    display_name = models.CharField(max_length=200, blank=True)

    def __str__(self):
        return f"{self.user.username} @ {self.tenant.slug}"
