# DEPLOY.md — clone to live URL

Two paths. **Local** takes about 10 minutes and gets you the numbers the README
asks for. **Render** takes about 20 minutes and gets you the public link.

Do local first: the measurements are the point, and you cannot take an honest
ingestion-throughput reading against a free-tier instance that sleeps.

---

## A. Local, with Docker — ~10 minutes

```bash
git clone https://github.com/Vb-777/thermaledger.git
cd thermaledger
cp .env.example .env
docker compose up -d db broker          # wait ~10s for the healthcheck

# ONE TIME, then commit the result. The repo ships models, not migration
# files, so this is the first command you run and the only one you never
# run again. CI checks that the output is committed.
docker compose run --rm web python manage.py makemigrations tenancy metering billing auditlog

docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py enable_rls
docker compose run --rm web python manage.py seed_demo
docker compose up -d web ingest
```

The API is on http://localhost:8000. Then the portal:

```bash
cd frontend
npm install
npm run dev                              # http://localhost:5173
```

Sign in as `acme-steel-portal` / `thermaledger-demo`. Two other logins exist:
`borax-chem-portal` and `veda-textiles-portal`, same password. **Sign in as two
of them in two browsers** — that is the demo. Same site codes, same meter
serials, completely different data.

### Without Docker

You need PostgreSQL 16 and Mosquitto locally. The one thing you cannot skip:

```sql
CREATE ROLE thermaledger_app WITH LOGIN PASSWORD 'thermaledger' NOSUPERUSER NOBYPASSRLS CREATEDB;
CREATE DATABASE thermaledger OWNER thermaledger_app;
```

Connecting as `postgres` will appear to work and will quietly disable every
RLS policy in the project. `enable_rls` refuses to run in that case rather than
letting you ship it.

---

## B. Render — ~20 minutes

1. Push to GitHub — **with the migration files from step A committed.** Render
   runs `migrate`, not `makemigrations`; without them the deploy builds an
   incomplete schema.
2. Render dashboard → **New → Blueprint** → pick the repo. `render.yaml` creates
   the Postgres instance, the API service and the static portal.
3. First deploy will partly fail: the two services do not know each other's
   URLs yet. After both exist, set:
   - on **thermaledger-api**: `CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS`
     → `https://thermaledger-portal.onrender.com`
   - on **thermaledger-portal**: `VITE_API_BASE`
     → `https://thermaledger-api.onrender.com`
4. Redeploy both.
5. Seed, from the API service's **Shell** tab:
   ```bash
   python manage.py seed_demo
   python manage.py run_billing --month 2026-09
   python manage.py run_billing --month 2026-10
   python manage.py verify_billing
   ```
6. Open the portal URL and sign in.

`preDeployCommand` runs `migrate` and `enable_rls` on every deploy. If
`enable_rls` aborts, the release is cancelled rather than going live with
isolation switched off.

### What the free tier actually costs you

Record these honestly; a CV bullet claiming production uptime on a sleeping
free instance is the kind of claim an interviewer will test.

| Item | Free tier | What it means |
|---|---|---|
| Web service | ₹0 | Sleeps after 15 min idle; 30–50 s cold start |
| Static site | ₹0 | Always on, CDN-served |
| PostgreSQL | ₹0 | **Expires after 30 days**; 1 GB |
| **Total** | **₹0/month** | |

Paid equivalent, if you want it to stay up: Starter web ₹600/mo + Starter
Postgres ₹600/mo ≈ **₹1,200/month**. Put whichever you actually run in the
README cost slot.

### Backups

Free Postgres has no automatic backups. One line, run from your laptop or a
GitHub Actions schedule:

```bash
pg_dump "$DATABASE_URL" | gzip > thermaledger-$(date +%F).sql.gz
```

Restore it into a scratch database at least once before you claim backups work
— an untested restore is not a backup. That tested restore is Shipyard's job
(build #2 in your list), not this one.

---

## C. Capturing the numbers the README wants

Run these in order. Each prints a line marked with its README slot.

```bash
# [TEST_COUNT] and [ISOLATION_TESTS]
pytest -q
pytest -m isolation -q

# [INVOICE_ACCURACY] and [AUDIT_CHAIN]
python manage.py run_billing --month 2026-09
python manage.py run_billing --month 2026-10
python manage.py verify_billing

# [INGEST_RATE] — terminal 1
python manage.py ingest_mqtt --max-messages 5000
#                 terminal 2
python scripts/meter_simulator.py --mode burst --count 5000

# [E2E_LATENCY] — ingest worker running, then:
python manage.py drf_create_token acme-steel-portal
python scripts/meter_simulator.py --mode latency --samples 50 --token <token>
```

Then fill the slots in README.md and commit. Those numbers are what the CV
bullet is made of — take them from your own run, not from this document.

---

## D. Troubleshooting

**`enable_rls` says the role can bypass RLS.** You are connected as a superuser.
That is the check working. Create `thermaledger_app` as above.

**Isolation tests fail in CI but pass locally.** Check which role CI connects
as — the workflow creates `thermaledger_app` deliberately.

**Portal shows "session has expired" immediately.** `VITE_API_BASE` is wrong or
`CORS_ALLOWED_ORIGINS` on the API does not include the portal's origin.

**Portal loads but every list is empty.** Usually the tenant binding: the user
has no `TenantUser` row, so no tenant is bound and the policy correctly returns
nothing. `python manage.py shell` → `TenantUser.objects.filter(user__username=...)`.

**Invoices show zero consumption.** The billing month has no readings in it.
`seed_demo` writes the last 90 days; bill a month inside that window.
