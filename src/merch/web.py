from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

import structlog
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.sessions import SessionMiddleware

from merch.artwork_replacement import has_unresolved_artwork_replacement
from merch.config import Settings, get_settings
from merch.copy_refresh import (
    approve_copy_refresh_batch,
    edit_copy_refresh_item,
    get_copy_refresh_batch,
    latest_copy_refresh_batch,
    retry_copy_refresh_batch,
)
from merch.database import get_db, get_engine, session_scope
from merch.domain.listing_copy import validate_listing_copy
from merch.domain.product_options import publication_template
from merch.models import (
    ArtifactRecord,
    ConnectorStateRecord,
    CsvImportRecord,
    OrderRecord,
    RunRecord,
)
from merch.observability import configure_observability
from merch.pipeline import ensure_fixture_template, health_connectors
from merch.repository import ConfigurationRepository, MetricsRepository, RunRepository
from merch.schemas import (
    ApprovalRequest,
    ApprovalSignal,
    Channel,
    ConnectorTokenUpdate,
    CopyRefreshApproval,
    CopyRefreshEdit,
    CreativeBrief,
    EtsyCsvImportResult,
    ListingPackageUpdate,
    ProductTemplate,
    PublishInput,
    RunStatus,
)
from merch.security import (
    LoginThrottle,
    client_identity,
    csrf_token,
    password_matches,
    require_admin,
    require_csrf,
    safe_json,
)
from merch.services.analytics import parse_etsy_stats_csv
from merch.services.credentials import CredentialCipher, CredentialStore
from merch.services.openai_costs import summarize_costs
from merch.services.storage import ArtifactStorage
from merch.temporal import (
    CopyRefreshApplyWorkflow,
    CopyRefreshPrepareWorkflow,
    MerchWorkflow,
    RetryPublishWorkflow,
    retry_failed_artwork_run,
    start_manual_run,
    temporal_client,
)

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=ROOT / "templates")
DatabaseSession = Annotated[Session, Depends(get_db)]
REQUESTS = Counter("merch_http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("merch_http_request_seconds", "HTTP request latency", ["method", "path"])


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ]
    )


def _run_payload(record: RunRecord) -> dict[str, Any]:
    production = [
        item for item in record.artifacts
        if item.kind == f"production-v{record.version}"
    ]
    latest_production = max(production, key=lambda item: item.revision) if production else None
    featured_color_selection = (
        latest_production.metadata_json.get("featured_color_selection")
        if latest_production else None
    )
    payload = {
        "id": record.id,
        "workflow_id": record.workflow_id,
        "status": record.status,
        "version": record.version,
        "scheduled_for": record.scheduled_for,
        "manual": record.manual,
        "pipeline_version": record.pipeline_version,
        "research_report": record.research_report,
        "selection": record.selection,
        "selected_concept": record.selected_concept,
        "creative_brief": record.creative_brief,
        "typography_spec": (
            latest_production.metadata_json.get("typography_spec")
            if latest_production else None
        ),
        "artwork_effects": (
            latest_production.metadata_json.get("artwork_effects")
            if latest_production else None
        ),
        "ip_report": record.ip_report,
        "qa_report": record.qa_report,
        "listings": record.listings,
        "listing_generation_state": record.listing_generation_state,
        "price_quotes": record.price_quotes,
        "template_snapshot": record.template_snapshot,
        "excluded_shirt_colors": record.excluded_shirt_colors or [],
        "publication_template_snapshot": record.publication_template_snapshot,
        "selected_opportunity": record.selected_opportunity,
        "reference_analysis": record.reference_analysis,
        "product_plan": record.product_plan,
        "originality_report": record.originality_report,
        "seo_evidence": record.seo_evidence,
        "price_decisions": record.price_decisions,
        "featured_color_selection": featured_color_selection,
        "replacement_shirt_colors": sorted(
            {item["color"] for item in (record.publication_template_snapshot or {}).get("variants", []) if item.get("enabled", True)}
            - {item["color"] for item in (record.template_snapshot or {}).get("variants", []) if item.get("enabled", True)}
        ),
        "provider_calls": record.provider_calls,
        "openai_cost": summarize_costs(record.provider_calls or []),
        "error": record.error,
        "concepts": [
            {
                "id": item.id,
                "rank": item.rank,
                "eligible": item.eligible,
                "rejection_reason": item.rejection_reason,
                "weighted_score": item.weighted_score,
                "selected": item.selected,
                "data": item.data,
                "score_breakdown": (record.selection or {}).get("score_breakdowns", {}).get(
                    item.data.get("concept_name", "")
                ),
            }
            for item in record.concepts
        ],
        "opportunities": [
            {
                "id": item.id,
                "rank": item.rank,
                "eligible": item.eligible,
                "rejection_reason": item.rejection_reason,
                "weighted_score": item.weighted_score,
                "data": item.data,
            }
            for item in record.opportunities
        ],
        "artifacts": [
            {
                "id": item.id,
                "kind": item.kind,
                "revision": item.revision,
                "sha256": item.sha256,
                "width": item.width,
                "height": item.height,
                "metadata": item.metadata_json,
            }
            for item in record.artifacts
        ],
        "approvals": [
            {
                "version": item.version,
                "actor": item.actor,
                "channels": item.channels,
                "ip_attested": item.ip_attested,
                "decision": item.decision,
                "created_at": item.created_at,
            }
            for item in record.approvals
        ],
        "publishes": [
            {
                "channel": item.channel,
                "status": item.status,
                "artwork_upload_id": item.artwork_upload_id,
                "printify_product_id": item.printify_product_id,
                "external_product_id": item.external_product_id,
                "response_data": item.response_data,
                "error": item.error,
            }
            for item in record.publishes
        ],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    return cast(dict[str, Any], jsonable_encoder(payload))


def _run_page_payload(record: RunRecord, ip_check_enabled: bool) -> dict[str, Any]:
    payload = _run_payload(record)
    if ip_check_enabled or record.pipeline_version == 2:
        return payload
    payload.pop("ip_report", None)
    payload["provider_calls"] = [
        call for call in payload["provider_calls"] if call.get("stage") != "ip_screen"
    ]
    for call in payload["provider_calls"]:
        call.pop("prompt", None)
    for approval in payload["approvals"]:
        approval.pop("ip_attested", None)
    if payload["research_report"]:
        for candidate in payload["research_report"]["candidates"]:
            candidate.get("scores", {}).pop("ip_risk", None)
    if payload["selected_concept"]:
        payload["selected_concept"].get("scores", {}).pop("ip_risk", None)
    for concept in payload["concepts"]:
        concept["data"].get("scores", {}).pop("ip_risk", None)
        if concept.get("score_breakdown"):
            concept["score_breakdown"].pop("ip_penalty", None)
    for breakdown in (payload.get("selection") or {}).get("score_breakdowns", {}).values():
        breakdown.pop("ip_penalty", None)
    return payload


def _load_run(db: Session, run_id: str) -> RunRecord:
    statement = (
        select(RunRecord)
        .where(RunRecord.id == run_id)
        .options(
            selectinload(RunRecord.concepts),
            selectinload(RunRecord.artifacts),
            selectinload(RunRecord.approvals),
            selectinload(RunRecord.publishes),
            selectinload(RunRecord.opportunities),
        )
    )
    record = db.scalar(statement)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return record


def _mockup_evidence(response: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    response = response or {}
    report = response.get("mockup_verification") or {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in [*(response.get("mockup_manifest") or []), *(report.get("checks") or [])]:
        color = str(entry.get("color") or "")
        if color:
            entries.setdefault(color, {"color": color}).update(entry)
    return entries


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    if settings.provider_mode == "fake":
        ensure_fixture_template(settings)
    yield


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Autonomous POD Production", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.login_throttle = LoginThrottle(settings.login_attempts_per_15_minutes)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="merch_session",
        max_age=8 * 60 * 60,
        same_site="lax",
        https_only=settings.app_env == "production",
    )
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    configure_observability(settings, app)

    @app.middleware("http")
    async def request_observability(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        started = time.monotonic()
        request_id = request.headers.get("x-request-id") or secrets.token_hex(12)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            structlog.get_logger().exception("request.failed", path=request.url.path)
            raise
        finally:
            structlog.contextvars.clear_contextvars()
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        REQUESTS.labels(request.method, route_path, response.status_code).inc()
        LATENCY.labels(request.method, route_path).observe(time.monotonic() - started)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' https://unpkg.com"
        )
        return response

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "database unavailable"
            ) from exc
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": csrf_token(request), "error": None},
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, password: Annotated[str, Form()]) -> Response:
        await require_csrf(request)
        identity = client_identity(request, settings)
        throttle: LoginThrottle = app.state.login_throttle
        throttle.check(identity)
        if not password_matches(password, settings):
            throttle.failure(identity)
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf_token": csrf_token(request), "error": "Invalid password"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        throttle.success(identity)
        request.session.clear()
        request.session["actor"] = "admin"
        csrf_token(request)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        require_admin(request)
        await require_csrf(request)
        request.session.clear()
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, db: DatabaseSession) -> Response:
        if request.session.get("actor") != "admin":
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        runs = RunRepository(db).list_runs()
        metrics_rows = MetricsRepository(db).recent(90)
        totals: dict[str, dict[str, int]] = {}
        for row in metrics_rows:
            channel = totals.setdefault(row.channel, {"orders": 0, "revenue": 0, "visits": 0})
            channel["orders"] += int(row.data.get("orders") or 0)
            channel["revenue"] += int(row.data.get("gross_revenue_cents") or 0)
            channel["visits"] += int(row.data.get("visits") or 0)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={"runs": runs, "totals": totals, "csrf_token": csrf_token(request)},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: str, db: DatabaseSession) -> Response:
        if request.session.get("actor") != "admin":
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        run = _load_run(db, run_id)
        product_template = (
            ProductTemplate.model_validate(run.template_snapshot)
            if run.template_snapshot
            else ConfigurationRepository(db).get_template()
        )
        product_template = publication_template(
            product_template, run.excluded_shirt_colors or [], run.publication_template_snapshot
        )
        emergency_design_review = RunRepository(db).has_current_audit_action(
            run_id, "artwork.typography_fallback", run.version
        )
        return templates.TemplateResponse(
            request=request,
            name="run.html",
            context={
                "run": run,
                "payload": _run_page_payload(run, settings.ip_check_enabled),
                "ip_check_enabled": settings.ip_check_enabled,
                "manual_review_required": (
                    settings.manual_approval_enabled
                    or settings.ip_check_enabled
                    or settings.etsy_production_partner_check_enabled
                    or emergency_design_review
                ),
                "emergency_design_review": emergency_design_review,
                "featured_variant": product_template.featured_variant().title,
                "mockup_evidence": {
                    item.channel: list(_mockup_evidence(item.response_data).values())
                    for item in run.publishes if item.channel == Channel.ETSY.value
                },
                "artwork_replacement_reconciliation_required": any(
                    item.channel == Channel.ETSY.value
                    and has_unresolved_artwork_replacement(item.response_data)
                    for item in run.publishes
                ),
                "csrf_token": csrf_token(request),
            },
        )

    @app.get("/connectors", response_class=HTMLResponse)
    async def connectors_page(request: Request, db: DatabaseSession) -> Response:
        if request.session.get("actor") != "admin":
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        states = list(db.scalars(select(ConnectorStateRecord).order_by(ConnectorStateRecord.name)))
        template = ConfigurationRepository(db).get_template()
        configured = {
            "Printify": bool(settings.printify_api_token.get_secret_value()),
            "Shopify analytics": bool(
                settings.shopify_shop_domain and settings.shopify_admin_token.get_secret_value()
            ),
            "Etsy analytics": bool(
                settings.etsy_api_key.get_secret_value() and settings.etsy_shop_id
            ),
            "Amazon analytics": bool(settings.amazon_lwa_client_id.get_secret_value()),
        }
        return templates.TemplateResponse(
            request=request,
            name="connectors.html",
            context={
                "states": states,
                "configured": configured,
                "product_template": template.model_dump(mode="json"),
                "featured_options": [item for item in template.variants if item.enabled],
                "csrf_token": csrf_token(request),
            },
        )

    @app.get("/copy-refresh", response_class=HTMLResponse)
    async def copy_refresh_page(request: Request) -> Response:
        if request.session.get("actor") != "admin":
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return templates.TemplateResponse(
            request=request, name="copy_refresh.html",
            context={"batch": latest_copy_refresh_batch(), "csrf_token": csrf_token(request)},
        )

    @app.get("/api/copy-refresh/latest")
    async def copy_refresh_latest(request: Request) -> dict[str, Any] | None:
        require_admin(request)
        return latest_copy_refresh_batch()

    @app.get("/api/copy-refresh/{batch_id}")
    async def copy_refresh_detail(request: Request, batch_id: str) -> dict[str, Any]:
        require_admin(request)
        try:
            return get_copy_refresh_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    @app.post("/api/copy-refresh/prepare", status_code=status.HTTP_202_ACCEPTED)
    async def copy_refresh_prepare(request: Request) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        client = await temporal_client(settings)
        workflow_id = f"copy-refresh-prepare-{uuid4()}"
        await client.start_workflow(
            CopyRefreshPrepareWorkflow.run, workflow_id,
            id=workflow_id, task_queue=settings.temporal_task_queue,
        )
        return {"status": "preparing", "workflow_id": workflow_id}

    @app.put("/api/copy-refresh/{batch_id}/items/{item_id}")
    async def copy_refresh_edit(
        request: Request, batch_id: str, item_id: str, value: CopyRefreshEdit
    ) -> dict[str, Any]:
        require_admin(request)
        await require_csrf(request)
        try:
            return edit_copy_refresh_item(batch_id, item_id, value)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    @app.post("/api/copy-refresh/{batch_id}/approve", status_code=status.HTTP_202_ACCEPTED)
    async def copy_refresh_approve(
        request: Request, batch_id: str, value: CopyRefreshApproval
    ) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        try:
            approve_copy_refresh_batch(batch_id, value.expected_version, value.digest)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        client = await temporal_client(settings)
        workflow_id = f"copy-refresh-apply-{batch_id}-{uuid4()}"
        await client.start_workflow(
            CopyRefreshApplyWorkflow.run, batch_id,
            id=workflow_id, task_queue=settings.temporal_task_queue,
        )
        return {"status": "applying", "workflow_id": workflow_id}

    @app.post("/api/copy-refresh/{batch_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    async def copy_refresh_retry(request: Request, batch_id: str) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        try:
            retry_copy_refresh_batch(batch_id)
        except KeyError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        client = await temporal_client(settings)
        workflow_id = f"copy-refresh-apply-{batch_id}-{uuid4()}"
        await client.start_workflow(
            CopyRefreshApplyWorkflow.run, batch_id,
            id=workflow_id, task_queue=settings.temporal_task_queue,
        )
        return {"status": "applying", "workflow_id": workflow_id}

    @app.get("/artifacts/{artifact_id}")
    async def artifact(request: Request, artifact_id: str, db: DatabaseSession) -> Response:
        require_admin(request)
        run = db.scalar(
            select(RunRecord)
            .join(RunRecord.artifacts)
            .where(ArtifactRecord.id == artifact_id)
            .options(selectinload(RunRecord.artifacts))
        )
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact not found")
        item = next(value for value in run.artifacts if value.id == artifact_id)
        data = ArtifactStorage(settings).get(item.object_key)
        return Response(data, media_type=item.content_type, headers={"ETag": item.sha256})

    @app.get("/api/runs")
    async def list_runs(request: Request, db: DatabaseSession) -> list[dict[str, Any]]:
        require_admin(request)
        return [
            RunRepository(db).view(item).model_dump(mode="json")
            for item in RunRepository(db).list_runs()
        ]

    @app.get("/api/catalog")
    async def catalog(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        from merch.repository import CatalogRepository

        with session_scope() as session:
            products = CatalogRepository(session).list_products()
        return [item.model_dump(mode="json") for item in products]

    @app.post("/api/catalog/sync")
    async def catalog_sync(request: Request) -> dict[str, Any]:
        require_admin(request)
        await require_csrf(request)
        from merch.services.catalog import sync_catalog

        return await sync_catalog(settings)

    @app.get("/api/research/sources/health")
    async def research_source_health(request: Request) -> dict[str, dict[str, Any]]:
        require_admin(request)
        from merch.services.marketplace_research import source_health

        return await source_health(settings)

    @app.get("/api/connectors/printify-browser/health")
    async def printify_browser_health(request: Request) -> dict[str, Any]:
        require_admin(request)
        from merch.services.browser_session import browser_session_health

        return browser_session_health(settings)

    @app.get("/api/research/smoke")
    async def research_smoke(request: Request, query: str) -> list[dict[str, Any]]:
        require_admin(request)
        if not query.strip() or len(query) > 200:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "query is invalid")
        from merch.services.marketplace_research import collect_marketplace_evidence

        evidence = await collect_marketplace_evidence(query.strip(), settings)
        return [item.model_dump(mode="json") for item in evidence]

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def new_run(request: Request) -> dict[str, Any]:
        require_admin(request)
        await require_csrf(request)
        try:
            value = await start_manual_run(settings)
        except Exception as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"Temporal unavailable: {exc}"
            ) from exc
        return value.model_dump(mode="json")

    @app.get("/api/runs/{run_id}")
    async def get_run(request: Request, run_id: str, db: DatabaseSession) -> dict[str, Any]:
        require_admin(request)
        return _run_payload(_load_run(db, run_id))

    @app.get("/api/runs/{run_id}/mockup-evidence/{side}")
    async def mockup_evidence_image(
        request: Request, run_id: str, side: str, db: DatabaseSession, color: str = "",
    ) -> Response:
        require_admin(request)
        if side not in {"source", "actual"}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mockup evidence not found")
        run = _load_run(db, run_id)
        publish = next((item for item in run.publishes if item.channel == Channel.ETSY.value), None)
        entry = _mockup_evidence(publish.response_data if publish else None).get(color, {})
        object_key = entry.get(f"{side}_object_key")
        content_type = entry.get(f"{side}_content_type")
        if not object_key or content_type not in {"image/png", "image/jpeg"}:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mockup evidence not found")
        try:
            data = ArtifactStorage(settings).get(object_key)
        except Exception as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mockup evidence not found") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry.get(f"{side}_sha256"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Mockup evidence not found")
        return Response(data, media_type=content_type, headers={
            "ETag": f'"{digest}"', "Cache-Control": "private, no-store",
        })

    @app.put("/api/runs/{run_id}/package")
    async def edit_package(
        request: Request,
        run_id: str,
        value: ListingPackageUpdate,
        db: DatabaseSession,
    ) -> dict[str, Any]:
        require_admin(request)
        await require_csrf(request)
        try:
            validate_listing_copy(value.listings)
            run_record = RunRepository(db).get(run_id)
            template = (
                ProductTemplate.model_validate(run_record.template_snapshot)
                if run_record.template_snapshot
                else ConfigurationRepository(db).get_template()
            )
            template = publication_template(
                template, run_record.excluded_shirt_colors or [],
                run_record.publication_template_snapshot,
            )
            configured = {
                (channel.channel, variant.variant_id): (channel, variant)
                for channel in template.channels
                if channel.enabled
                for variant in template.variants
                if variant.enabled
            }
            supplied = {(quote.channel, quote.variant_id) for quote in value.prices}
            if supplied != set(configured):
                raise ValueError("prices must cover every enabled channel and variant exactly once")
            for quote in value.prices:
                channel, variant = configured[(quote.channel, quote.variant_id)]
                fee = (
                    round(quote.retail_price_cents * channel.percent_fee) + channel.fixed_fee_cents
                )
                margin = (
                    quote.retail_price_cents - variant.production_cost_cents - fee
                ) / quote.retail_price_cents
                if quote.production_cost_cents != variant.production_cost_cents:
                    raise ValueError("production costs must match the active Printify snapshot")
                if quote.retail_price_cents % 100 != 99 or margin < settings.target_margin:
                    raise ValueError("edited prices must end in .99 and preserve the target margin")
            run = RunRepository(db).update_package(
                run_id,
                expected_version=value.expected_version,
                listings=value.listings.model_dump(mode="json"),
                quotes=[item.model_dump(mode="json") for item in value.prices],
                actor="admin",
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"id": run.id, "version": run.version, "status": run.status}

    @app.post("/api/runs/{run_id}/approve", status_code=status.HTTP_202_ACCEPTED)
    async def approve(request: Request, run_id: str, value: ApprovalRequest) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        if settings.ip_check_enabled and not value.ip_attested:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "IP attestation is required")
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if run.status != RunStatus.AWAITING_APPROVAL.value:
                raise HTTPException(status.HTTP_409_CONFLICT, "Run is not awaiting approval")
        client = await temporal_client(settings)
        signal = ApprovalSignal(
            channels=value.channels,
            expected_version=value.expected_version,
            ip_attested=value.ip_attested,
            actor="admin",
        )
        await client.get_workflow_handle(run.workflow_id).signal(MerchWorkflow.approve, signal)
        return {"status": "approval_signaled"}

    async def _simple_signal(request: Request, run_id: str, signal_name: str) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        with session_scope() as db:
            run = RunRepository(db).get(run_id)
            if (
                signal_name in {"reject", "regenerate"}
                and run.status != RunStatus.AWAITING_APPROVAL.value
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "Run is not awaiting operator review")
            if signal_name == "cancel" and run.status in {
                RunStatus.PUBLISHED.value,
                RunStatus.REJECTED.value,
                RunStatus.CANCELLED.value,
                RunStatus.FAILED.value,
                RunStatus.NO_SAFE_CANDIDATE.value,
                RunStatus.NO_QUALIFIED_OPPORTUNITY.value,
            }:
                raise HTTPException(status.HTTP_409_CONFLICT, "Run is already terminal")
        client = await temporal_client(settings)
        handle = client.get_workflow_handle(run.workflow_id)
        if signal_name == "reject":
            await handle.signal(MerchWorkflow.reject, "admin")
        elif signal_name == "regenerate":
            await handle.signal(MerchWorkflow.regenerate)
        else:
            await handle.signal(MerchWorkflow.cancel)
        return {"status": f"{signal_name}_signaled"}

    @app.post("/api/runs/{run_id}/reject", status_code=status.HTTP_202_ACCEPTED)
    async def reject(request: Request, run_id: str) -> dict[str, str]:
        return await _simple_signal(request, run_id, "reject")

    @app.post("/api/runs/{run_id}/regenerate", status_code=status.HTTP_202_ACCEPTED)
    async def regenerate(request: Request, run_id: str) -> dict[str, str]:
        return await _simple_signal(request, run_id, "regenerate")

    @app.post("/api/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel(request: Request, run_id: str) -> dict[str, str]:
        return await _simple_signal(request, run_id, "cancel")

    @app.post("/api/runs/{run_id}/retry/{channel}", status_code=status.HTTP_202_ACCEPTED)
    async def retry_channel(request: Request, run_id: str, channel: Channel) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        with session_scope() as db:
            run = RunRepository(db).get(run_id, full=True)
            retryable = next(
                (item for item in run.publishes if item.channel == channel.value), None
            )
            if (
                channel == Channel.ETSY
                and retryable is not None
                and has_unresolved_artwork_replacement(retryable.response_data)
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Published artwork replacement requires the dedicated "
                    "reconcile-published-artwork command",
                )
            if (
                run.status not in {
                    RunStatus.PARTIALLY_PUBLISHED.value,
                    RunStatus.FAILED.value,
                    RunStatus.VERIFICATION_REQUIRED.value,
                }
                or retryable is None
                or retryable.status not in {"failed", "reconciliation_required"}
            ):
                raise HTTPException(status.HTTP_409_CONFLICT, "Run has no retryable channel")
        client = await temporal_client(settings)
        retry_id = f"merch-retry-{run_id}-{channel.value}-{int(time.time())}"
        await client.start_workflow(
            RetryPublishWorkflow.run,
            PublishInput(run_id=UUID(run_id), channel=channel),
            id=retry_id,
            task_queue=settings.temporal_task_queue,
        )
        return {"status": "retry_started", "workflow_id": retry_id}

    @app.post("/api/runs/{run_id}/retry-artwork", status_code=status.HTTP_202_ACCEPTED)
    async def retry_artwork_with_brief(
        request: Request, run_id: str, value: CreativeBrief
    ) -> dict[str, str]:
        require_admin(request)
        await require_csrf(request)
        try:
            restarted = await retry_failed_artwork_run(run_id, revised_brief=value)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"status": "retry_started", "run_id": str(restarted.run_id)}

    @app.get("/api/template")
    async def get_template(request: Request, db: DatabaseSession) -> dict[str, Any]:
        require_admin(request)
        return ConfigurationRepository(db).get_template().model_dump(mode="json")

    @app.put("/api/template")
    async def save_template(
        request: Request, value: ProductTemplate, db: DatabaseSession
    ) -> dict[str, Any]:
        require_admin(request)
        await require_csrf(request)
        repository = ConfigurationRepository(db)
        try:
            current = repository.get_template_record()
        except RuntimeError:
            record = repository.save_template(value)
        else:
            try:
                record = repository.activate_template(value, expected_version=current.version)
            except ValueError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        RunRepository(db).audit(None, "admin", "template.saved", {"version": record.version})
        return {"version": record.version, "template": value.model_dump(mode="json")}

    @app.get("/api/connectors")
    async def connector_status(request: Request) -> dict[str, str]:
        require_admin(request)
        return await health_connectors(settings)

    @app.put("/api/connectors/token", status_code=status.HTTP_204_NO_CONTENT)
    async def store_connector_token(
        request: Request, value: ConnectorTokenUpdate, db: DatabaseSession
    ) -> Response:
        require_admin(request)
        await require_csrf(request)
        try:
            store = CredentialStore(
                db, CredentialCipher(settings.credential_encryption_key.get_secret_value())
            )
            store.set(value.credential, value.value)
            if value.credential in {"etsy_access_token", "etsy_refresh_token"}:
                store.set("etsy_access_token_expires_at", "0")
        except RuntimeError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
        RunRepository(db).audit(
            None, "admin", "connector.credential_updated", {"credential": value.credential}
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/imports/etsy", response_model=EtsyCsvImportResult)
    async def import_etsy(
        request: Request,
        file: Annotated[UploadFile, File()],
        db: DatabaseSession,
    ) -> EtsyCsvImportResult:
        require_admin(request)
        await require_csrf(request)
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "CSV exceeds 10 MB")
        digest = hashlib.sha256(data).hexdigest()
        if db.scalar(select(CsvImportRecord).where(CsvImportRecord.sha256 == digest)):
            raise HTTPException(status.HTTP_409_CONFLICT, "This CSV was already imported")
        try:
            rows = parse_etsy_stats_csv(data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        metrics_repo = MetricsRepository(db)
        for metric in rows:
            metrics_repo.upsert(metric)
        filename = Path(file.filename or "etsy-stats.csv").name
        db.add(
            CsvImportRecord(
                channel=Channel.ETSY.value,
                filename=filename,
                sha256=digest,
                rows_imported=len(rows),
                rows_rejected=0,
                actor="admin",
            )
        )
        RunRepository(db).audit(
            None, "admin", "etsy.csv_imported", {"sha256": digest, "rows": len(rows)}
        )
        return EtsyCsvImportResult(
            filename=filename, sha256=digest, imported_rows=len(rows), rejected_rows=0
        )

    @app.post("/webhooks/printify", status_code=status.HTTP_202_ACCEPTED)
    async def printify_webhook(request: Request) -> dict[str, str]:
        secret = settings.printify_webhook_secret.get_secret_value()
        if not secret:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook secret is not configured"
            )
        body = await request.body()
        supplied = request.headers.get("x-pfy-signature", "")
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied.removeprefix("sha256="), expected):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")
        payload = json.loads(body)
        resource = payload.get("resource", payload.get("data", {}))
        order_id = str(resource.get("id", ""))
        if order_id:
            with session_scope() as db:
                try:
                    template = ConfigurationRepository(db).get_template()
                    shop_id = str(payload.get("shop_id", resource.get("shop_id", "")))
                    event_channel = next(
                        (
                            item.channel.value
                            for item in template.channels
                            if item.printify_shop_id == shop_id
                        ),
                        "printify",
                    )
                except RuntimeError:
                    event_channel = "printify"
                order = db.scalar(
                    select(OrderRecord).where(
                        OrderRecord.channel == event_channel,
                        OrderRecord.external_order_id == order_id,
                    )
                )
                if order is None:
                    order = OrderRecord(
                        channel=event_channel,
                        external_order_id=order_id,
                        status=str(resource.get("status", "unknown")),
                    )
                    db.add(order)
                order.status = str(resource.get("status", order.status))
                order.quantity = sum(
                    int(item.get("quantity", 0)) for item in resource.get("line_items", [])
                )
                order.source_data = safe_json(resource)
        return {"status": "accepted"}

    return app


app = create_app()
