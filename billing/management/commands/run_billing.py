import calendar
import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from billing.engine import run_billing_for_tenant
from tenancy.models import Tenant


class Command(BaseCommand):
    help = (
        "Issue invoices for a billing month. Safe to re-run: idempotency keys "
        "mean a repeat produces no second invoice."
    )

    def add_arguments(self, parser):
        parser.add_argument("--month", required=True, help="YYYY-MM")
        parser.add_argument("--tenant", help="Tenant slug. Omit to bill every active tenant.")
        parser.add_argument("--actor", default="billing-runner")

    def handle(self, *args, **opts):
        try:
            year, month = (int(p) for p in opts["month"].split("-"))
            period_start = dt.date(year, month, 1)
        except ValueError as exc:
            raise CommandError(f"--month must be YYYY-MM: {exc}") from exc
        period_end = period_start + dt.timedelta(days=calendar.monthrange(year, month)[1])

        tenants = Tenant.objects.filter(is_active=True)
        if opts["tenant"]:
            tenants = tenants.filter(slug=opts["tenant"])
            if not tenants.exists():
                raise CommandError(f"no active tenant with slug {opts['tenant']!r}")

        totals = {"issued": 0, "replayed": 0, "failed": 0}
        for tenant in tenants.order_by("slug"):
            issued, replayed, failed = run_billing_for_tenant(
                tenant, period_start, period_end, actor=opts["actor"]
            )
            totals["issued"] += len(issued)
            totals["replayed"] += len(replayed)
            totals["failed"] += len(failed)
            self.stdout.write(
                f"{tenant.slug}: issued={len(issued)} replayed={len(replayed)} failed={len(failed)}"
            )
            for code, reason in failed:
                self.stderr.write(self.style.WARNING(f"  {code}: {reason}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{opts['month']}: {totals['issued']} issued, "
                f"{totals['replayed']} replayed (no duplicate charge), "
                f"{totals['failed']} failed"
            )
        )
