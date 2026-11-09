# ThermaLedger

Metering, billing and customer portal for Steam-as-a-Service and
Thermal-Energy-as-a-Service. Meter readings arrive over MQTT, land in
PostgreSQL as time series, become tariff-based invoices with an auditable
trail, and reach customers through a React portal where one customer
cannot see another's data.

**Live:** Not deployed publicly yet Â· demo login `acme-steel-portal` / `thermaledger-demo`

---

## Results

> Fill each slot from your own run. `DEPLOY.md` section C lists the exact
> command that prints each one. Do not ship this file with the brackets in it.

| | |
|---|---|
| Tenant isolation | Automated isolation tests prove a cross-tenant read returns zero rows at the database level |
| Invoice accuracy | Automated recomputation tests by an independent recomputation |
| Ingestion throughput | Benchmark pending |
| End-to-end latency | `[E2E_LATENCY]` p50 / p95, meter publish to visible in the API |
| Audit chain | Integrity tests verify the audit chain |
| Test suite | Automated test suite included |
| Infrastructure cost | Deployment-dependent |

---

## The part worth reading: tenant isolation

A customer portal where one customer can see another's consumption is not a bug
you fix later. So isolation here is two independent layers, and the tests prove
the lower one on its own.

**Layer 1 â€” PostgreSQL row-level security.** Every tenant-owned table carries a
`tenant_id` and a forced RLS policy:

```sql
USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
```

Each request binds its transaction to one tenant with `set_config(...)`. A query
that forgets its filter returns nothing. A query with the *wrong* filter returns
nothing. An `INSERT` tagged with another tenant is rejected by `WITH CHECK`, so
isolation is not read-side only.

There is no bypass flag. Unbound connection means `current_setting` is NULL,
which means zero rows â€” fail-closed, not fail-open. Code that legitimately spans
tenants (the billing runner, the seeder, the admin) loops over tenants and
enters each context in turn. Nothing can be handed cross-tenant visibility
because no such mode exists.

**Layer 2 â€” application scoping.** Every queryset goes through
`.for_current_tenant()`. The common path never *relies* on the database catching
a mistake; the ORM emits the tenant predicate and the planner gets an
index-friendly filter.

### Why the tests are written the way they are

A test asserting an endpoint returns 403 would pass with RLS switched off
entirely â€” the view's own permission check would carry it. So
`tests/test_rls_boundary.py` opens a cursor, binds to tenant A, and issues raw
SQL naming tenant B's primary keys. No ORM, no manager, no view in the call
stack. If a row comes back, isolation has failed.

The suite's first test asserts a precondition rather than assuming it: Postgres
exempts superusers and `BYPASSRLS` roles from every policy, **silently**. Run
the suite as `postgres` and all of it passes while enforcing nothing. So the
application connects as a dedicated non-superuser role, `enable_rls` refuses to
run if it can bypass RLS, CI creates that role deliberately, and the first
isolation test checks it. That failure mode is the most dangerous thing about
RLS and the easiest to not notice.

What the suite covers: read by primary key, unqualified `SELECT *`, aggregates,
joins, scalar subqueries, `UPDATE`, `DELETE`, `INSERT` tagged to another tenant,
re-tagging a row to another tenant, the unbound connection, a malformed setting,
and the context manager's own nesting and exception behaviour.

---

## How billing works

Three rules, applied in order:

1. **Consumption** = the difference between the bounding readings of the period.
   Readings are cumulative, so a dropped packet costs resolution, not kWh. A
   reading lower than its predecessor is a register reset, counted from zero â€”
   not negative consumption, which would credit the customer for a hardware
   event.
2. **Billed units** = `max(consumption, contracted minimum offtake)`. When the
   floor applies, the invoice says so on its own line; a customer paying for
   units they did not use should be able to see why.
3. **Price** = slab bands, each at its own rate, then the fixed charge, then
   adjustments, then tax on the result.

Money is integer paise end to end. Each line rounds once, half-up, so the
printed lines always add up to the printed total â€” the property
`verify_billing` checks across every invoice.

**Charge-once.** Issuance is keyed on `(tenant, site, period)`. A second billing
run is handed the first run's invoice and writes an `invoice.replayed` audit
event. The unique constraint is the mechanism, not a check the runner has to
remember. (Carried over from Paylink.)

**Auditable.** Every issuance, replay and adjustment appends to a per-tenant
hash chain: each entry hashed together with the one before it. Editing a past
entry invalidates everything after it, and `verify_chain` finds the break.
Recomputing the edited row's own hash is not enough â€” the next row still carries
the old `prev_hash`, and there is a test for exactly that. (Carried over from
AgentGuard.)

**Independently verified.** `manage.py verify_billing` does not call the billing
engine. It recomputes every invoice from raw readings with a second, plainer
implementation and compares. An engine bug consistent with itself still gets
caught.

---

## Architecture

```
meters â”€â”€MQTT (mTLS, per-device certs)â”€â”€â–¶ broker
                                            â”‚
                                   ingest_mqtt worker
                                            â”‚ tenant_context(...)
                                            â–¼
                                   PostgreSQL + RLS
                                            â”‚
                              billing engine â”€â”€â–¶ invoices + audit chain
                                            â”‚
                                   Django REST API
                                            â”‚ Token auth â†’ tenant binding
                                            â–¼
                                   React portal (Vite, Chart.js)
```

Device identity: the meter serial is the CN of the device certificate used for
mutual TLS on the broker, so the identity the broker authenticates is the
identity readings are filed under. A meter marked quarantined stops being
accepted at ingestion *and* stops contributing to invoices â€” both tested.
(Model carried over from TorqueTrust.)

| | |
|---|---|
| Backend | Django 5, Django REST Framework, PostgreSQL 16 |
| Ingestion | paho-mqtt, Mosquitto |
| Frontend | React 18, Vite, Chart.js |
| Infra | Docker, Docker Compose, Render, GitHub Actions |

---

## Running it

See **DEPLOY.md**. Short version:

```bash
cp .env.example .env
docker compose up -d db broker
docker compose run --rm web python manage.py makemigrations tenancy metering billing auditlog  # once, then commit
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py enable_rls
docker compose run --rm web python manage.py seed_demo
docker compose up -d web ingest
cd frontend && npm install && npm run dev
```

Three demo tenants, eight sites, fourteen meters, 90 days of hourly readings.
Two of the tenants deliberately share site codes and meter serials, so the demo
would expose an isolation bug rather than hide one.

```bash
pytest                      # everything
pytest -m isolation -v      # just the cross-tenant suite
python manage.py enable_rls --check
```

---

## Layout

```
config/          settings, urls
tenancy/         Tenant, RLS policy management, tenant_context, middleware, scoped managers
metering/        Site, Meter, Reading; transport-independent ingestion; MQTT worker
billing/         tariffs, slabs, contracts, adjustments, invoices, idempotency; the engine
auditlog/        hash-chained audit events
api/             DRF serializers, viewsets, consumption series, summary
frontend/        React portal
tests/           isolation, billing, idempotency, audit, ingestion, API
scripts/         meter simulator: stream, burst (throughput), latency
```

---

## What this is not

Honest scope, since the gap between a portfolio build and a production system is
where interviews go wrong:

- **No PDF invoices.** Invoices render in the portal; there is no rendering
  pipeline.
- **One ops summary page, not an operations dashboard.** Admin CRUD goes through
  Django admin.
- **No payment, SMS, email or maps integration.** That is a separate build.
- **No tested restore.** `pg_dump` is one line in DEPLOY.md, but an untested
  restore is not a backup, and this does not claim one.
- **Free-tier hosting sleeps.** The web service wakes in 30â€“50 seconds and the
  free database expires after 30 days. Fine for a demo link; not fine for a
  customer. The paid figure is in DEPLOY.md.
- **The MQTT broker runs without TLS locally.** The mutual-TLS path is
  implemented and configurable (`ingest_mqtt --tls-ca/--tls-cert/--tls-key`) but
  the demo does not provision a CA.

