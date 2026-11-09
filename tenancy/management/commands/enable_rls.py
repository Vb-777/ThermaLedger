from django.core.management.base import BaseCommand, CommandError

from tenancy import db


class Command(BaseCommand):
    help = (
        "Enable and (re)create row-level security policies on every tenant table. "
        "Idempotent; safe to run on every deploy."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Report which tables are missing a policy and exit non-zero, without changing anything.",
        )

    def handle(self, *args, **options):
        if options["check"]:
            return self._check()
        try:
            applied = db.enable_rls(verbose=True)
        except db.RlsBypassError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"RLS enforced on {len(applied)} tables"))

    def _check(self):
        from django.db import connection

        try:
            db.assert_role_cannot_bypass_rls()
        except db.RlsBypassError as exc:
            raise CommandError(str(exc)) from exc

        missing = []
        with connection.cursor() as cur:
            for table in db.RLS_TABLES:
                cur.execute(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, "
                    "       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
                    "FROM pg_class c WHERE c.relname = %s",
                    [table],
                )
                row = cur.fetchone()
                if row is None:
                    missing.append(f"{table}: table does not exist")
                    continue
                enabled, forced, policies = row
                if not (enabled and forced and policies):
                    missing.append(
                        f"{table}: enabled={enabled} forced={forced} policies={policies}"
                    )
        if missing:
            for line in missing:
                self.stderr.write(self.style.ERROR(line))
            raise CommandError(f"{len(missing)} table(s) are not protected")
        self.stdout.write(
            self.style.SUCCESS(f"all {len(db.RLS_TABLES)} tenant tables protected")
        )
