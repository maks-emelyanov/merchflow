# Catalog v2 operations

Catalog v2 is the default workflow for new scheduled and manual runs. It refreshes only the
Printify products entering research, researches listing-specific marketplace evidence, prepares
up to three ranked opportunities, and publishes at most one Etsy listing. A separate resumable
weekly workflow discovers every accessible provider offering for the blueprints in Printify's
current public Bestsellers ranking. Historical v1 runs retain their saved T-shirt and multi-channel
packages.

## Safety model

`MERCH_PROVIDER_MODE=fake` and `MERCH_PUBLISH_MODE=dry_run` are the safe local defaults. A v2 run
can mutate Etsy only when both provider and publish modes are live.

Every live v2 release requires all of the following:

- at least three specific competitor listings across at least two marketplaces;
- one explicit sales signal, or two independent proxy signals, within the configured freshness
  window;
- a compatible Printify blueprint/provider and supported decoration method;
- authenticated, account-specific Printify costs for every selected variant;
- when IP screening is enabled, a result of `pass` within the configured risk threshold;
- when originality screening is enabled, the configured originality and copying-risk thresholds
  for flat artwork and mockups;
- a contribution margin of at least 40% for every variant;
- deterministic Etsy SEO, disclosure, taxonomy, variation, and inventory validation;
- the unchanged package digest recorded by the system approval; and
- preactivation Etsy gallery pixel verification followed by active listing, inventory, SKU,
  price, option, and Printify-link readback.

Search fallback can satisfy competitor-evidence gates when it still provides specific listing
URLs and enough signals. It never supplies Printify costs or Etsy verification. Unknown
decoration methods, incomplete shipping, expired browser sessions, access challenges, identity
mismatches, or stale costs fail closed.

V2 IP and originality screening are optional and off by default. Set
`MERCH_IP_CHECK_ENABLED=true` and/or `MERCH_ORIGINALITY_CHECK_ENABLED=true` to opt in. A disabled
screen makes no screening model call, creates no screening report, and adds no corresponding
package gate; the normal reference-pattern analysis still runs. Enabled results and gate decisions
are bound into the exact package digest. `MERCH_MANUAL_APPROVAL_ENABLED` continues to control v1
runs.

## Initial setup

Run migrations and keep v2 in dry-run mode:

```bash
uv run merch migrate
```

Configure at minimum:

```dotenv
MERCH_PIPELINE_VERSION=2
MERCH_PROVIDER_MODE=live
MERCH_PUBLISH_MODE=dry_run
MERCH_IP_CHECK_ENABLED=false
MERCH_ORIGINALITY_CHECK_ENABLED=false
MERCH_CREDENTIAL_ENCRYPTION_KEY=<random-secret>
MERCH_PRINTIFY_API_TOKEN=<token>
MERCH_PRINTIFY_REQUEST_INTERVAL_SECONDS=0.12
MERCH_PRINTIFY_CATALOG_REQUEST_INTERVAL_SECONDS=0.65
MERCH_CATALOG_REFRESH_WEEKDAY=0
MERCH_CATALOG_REFRESH_HOUR=2
MERCH_CATALOG_REFRESH_MINUTE=0
MERCH_PRINTIFY_SHOP_ETSY=<shop-id>
MERCH_ETSY_API_KEY=<key>
MERCH_ETSY_SHARED_SECRET=<secret>
MERCH_ETSY_REFRESH_TOKEN=<refresh-token>
MERCH_ETSY_SHOP_ID=<shop-id>
```

The active product template remains the source for the Etsy shop and the shop-owned shipping,
return, readiness, and production-partner profile defaults. Import those defaults from a verified
listing created by this app before a live v2 release:

```bash
uv run merch import-etsy-defaults <listing-id>
```

Create the encrypted Printify browser session from a workstation with a graphical browser. The
command stores browser storage state, never the password:

```bash
uv run merch connect-browser printify
```

The default is two Etsy variation axes. Set `MERCH_ETSY_MAX_VARIATIONS_SUPPORTED=3` only after the
connected shop is confirmed to support a third variation. Variant selection still enforces the
appropriate inventory product limit and collapses additional Printify axes.

Catalog calls share a four-request concurrency limit and start at least
`MERCH_PRINTIFY_CATALOG_REQUEST_INTERVAL_SECONDS` apart. The 0.65-second default stays below
Printify's 100 catalog requests/minute limit and all safe reads honor `Retry-After`.

## Bootstrap and health checks

These commands do not publish a product:

```bash
uv run merch session-health
uv run merch source-health
uv run merch catalog-sync
uv run merch catalog-sync-status
uv run merch catalog-curate
uv run merch catalog-recover-providers
uv run merch research-smoke "ceramic mug"
```

An empty installation must finish `catalog-sync` before starting a production run. The CLI
waits by default; pass `--no-wait` to return the sync ID immediately and use
`catalog-sync-status [SYNC_ID]` to inspect it. Refreshes intersect Printify's full blueprint list
with the ordered public Bestsellers result, enumerate all accessible provider offerings for those
ranked blueprints, checkpoint every 50 offerings, and resume after failure. Unranked blueprints are
retained for history but are inactive and ineligible for research. Only successful completion
retires products absent from the discovery manifest; a confirmed unavailable offering is recorded
and soft-deactivated when its batch commits. An isolated `404` is recorded and skipped. Daily runs
use the cached catalog and live-refresh only enough viable products to research 25 candidates. The
chosen product is refreshed again before preparation and publication.

After a successful refresh, the worker deterministically selects one high-quality product per broad
category and uses the exact Printify Bestseller position as its research priority. The curation is
applied automatically. `catalog-curate` previews the current selection, while
`catalog-curate --apply` replaces it explicitly. `GET /api/catalog` returns this effective curated
set once curation exists, not every historical catalog row.

The worker schedules the Bestseller refresh every Sunday at 02:00 America/New_York by default. A
failed refresh retains its manifest and cursor, and the next manual or scheduled trigger resumes
it. Products untouched by an incomplete refresh remain active. A provider that remains inaccessible
after the bounded discovery retries is preserved rather than retired and appears in the refresh
warnings. After the provider is available again, run
`catalog-recover-providers [--sync-id SYNC_ID]`; it discovers and fetches only missing ranked
offerings and does not refresh already active pairs.

Equivalent authenticated endpoints are:

- `GET /api/catalog`
- `POST /api/catalog/sync` with the session CSRF token; returns `202` and a sync ID
- `GET /api/catalog/syncs`
- `GET /api/catalog/syncs/{sync_id}`
- `GET /api/connectors/printify-browser/health`
- `GET /api/research/sources/health`
- `GET /api/research/smoke?query=ceramic%20mug`

## Dry-run acceptance

Start the web, Temporal, and worker processes, then create a manual run from the UI or CLI:

```bash
uv run merch run
```

The run page exposes ranked opportunities, listing URLs, limitations, match rationale, reference
analysis, surface artwork, enabled originality/IP results, per-variant pricing, SEO provenance,
package digest, and publication verification.

A successful dry run finishes as `published` with an Etsy publication record whose status is
`dry_run`. It does not create an Etsy listing. Review at least the following before enabling live
publication:

- all five source-health results and fallback frequency;
- supported decoration methods from catalog sync;
- current cost coverage and cost age;
- opportunity evidence quality and catalog-match rationale;
- rejected opportunity reasons;
- achieved margin and undercut status;
- flat-artwork and mockup originality findings, when enabled; and
- the generated inventory axes, gallery coverage, copy, and disclosures.

## Live rollout

Enable `MERCH_PUBLISH_MODE=live` only after dry-run acceptance and a verified Etsy OAuth grant.
Start with one manual run. A successful live run creates a Printify product and Etsy draft,
verifies the draft gallery pixels, activates the Etsy listing, links it back to Printify, and then
performs final listing and inventory readback.

Do not enable unattended scheduled live publication until every decoration method currently
reported by `catalog-sync` is either covered by a contract fixture or intentionally fails closed.
Track source confidence, selector failures, cost age, enabled IP/originality rejections, achievable
margin, undercut rate, opportunity exhaustion, and Etsy verification failures during rollout.

## Recovery and terminal states

Temporal retries reuse stored research, reference images, model outputs, artwork, product IDs,
Etsy draft IDs, and image upload checkpoints. Completed publication activity replays return the
existing result. An uncertain create or upload is never blindly repeated.

- `no_qualified_opportunity` means no researched candidate passed within the configured maximum
  attempts. No listing was published.
- `verification_required` means a remote mutation may have occurred but final identity or
  storefront state could not be proven. Inspect the publication checkpoint and both providers
  before retrying.
- `reconciliation_required` on the Etsy publication record means the create/link outcome is
  ambiguous or a post-create check failed. Do not manually start a duplicate run for the same
  package.
- A Printify cost change at the final publication boundary rejects that opportunity and returns to
  ranking rather than publishing with stale economics.

Browser sessions marked unhealthy must be reconnected with `connect-browser printify`; collectors
do not solve CAPTCHAs or bypass authentication controls.

## Verification suite

```bash
uv run ruff check src/merch tests alembic
uv run mypy src/merch
uv run pytest
```

Saved-page contracts live under `tests/fixtures/marketplaces` and `tests/fixtures/printify`.
`tests/test_catalog_v2.py` covers marketplace extraction, challenges and freshness, cost selector
drift, pricing, generic inventory, multi-surface payloads, optional originality gates and mandatory
prepress gates, the complete fake-provider workflow, and publication replay.
