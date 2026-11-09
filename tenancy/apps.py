from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _apply_rls(sender, **kwargs):
    """Re-assert the RLS policies after every migrate.

    This is what guarantees the test database has policies on it. Without the
    hook, `pytest` would build a fresh schema with no RLS and the isolation
    suite would pass vacuously - the exact failure mode the suite exists to
    rule out.
    """
    from . import db

    db.enable_rls()


class TenancyConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenancy"

    def ready(self):
        post_migrate.connect(_apply_rls, sender=self, dispatch_uid="tenancy.enable_rls")
