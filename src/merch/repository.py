from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session, selectinload

from merch.domain.concept_ranking import concept_score_breakdown
from merch.domain.performance import summarize_performance
from merch.models import (
    ApprovalRecord,
    ArtifactRecord,
    AuditEvent,
    BrowserSessionRecord,
    CatalogProductRecord,
    CatalogVariantRecord,
    CompetitorListingRecord,
    ConceptRecord,
    ConnectorStateRecord,
    CostObservationRecord,
    DailyMetricRecord,
    OpportunityRecord,
    ProductMappingRecord,
    ProductTemplateRecord,
    PublishRecord,
    RunRecord,
)
from merch.schemas import (
    ApprovalSignal,
    CandidateConcept,
    CatalogProduct,
    CompetitorListingSnapshot,
    DailyPerformance,
    OriginalityReport,
    PriceDecision,
    ProductOpportunity,
    ProductPlanV2,
    ProductTemplate,
    ReferenceAnalysis,
    RunInput,
    RunStatus,
    RunView,
    SEOEvidence,
)


def lock_active_template(session: Session) -> ProductTemplateRecord | None:
    """Serialize catalog changes with run starts and terminal-run resumes.

    Call before reading state that will decide whether a run can resume. SQLite
    omits FOR UPDATE, so a no-op UPDATE acquires its transaction-wide write lock
    without modifying template data, versions, snapshots, or timestamps.
    """
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("UPDATE product_templates SET active = active WHERE active = 1"))
    return session.scalar(
        select(ProductTemplateRecord)
        .where(ProductTemplateRecord.active.is_(True))
        .order_by(ProductTemplateRecord.version.desc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )


class RunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, value: RunInput, workflow_id: str) -> RunRecord:
        # Serialize run creation with a catalog switch: after this transaction
        # commits, activation sees the pending run and cannot change its garment.
        lock_active_template(self.session)
        existing = self.session.get(RunRecord, str(value.run_id))
        if existing:
            return existing
        record = RunRecord(
            id=str(value.run_id),
            workflow_id=workflow_id,
            scheduled_for=value.scheduled_for,
            manual=value.manual,
            pipeline_version=value.pipeline_version,
            status=RunStatus.PENDING.value,
        )
        self.session.add(record)
        self.audit(str(value.run_id), "system", "run.created", {"workflow_id": workflow_id})
        self.session.flush()
        return record

    def get(self, run_id: str, *, full: bool = False) -> RunRecord:
        statement = select(RunRecord).where(RunRecord.id == run_id)
        if full:
            statement = statement.options(
                selectinload(RunRecord.concepts),
                selectinload(RunRecord.artifacts),
                selectinload(RunRecord.approvals),
                selectinload(RunRecord.publishes),
                selectinload(RunRecord.opportunities),
            )
        record = self.session.scalar(statement)
        if record is None:
            raise KeyError(f"run {run_id} not found")
        return record

    def has_audit_action(self, run_id: str, action: str) -> bool:
        """Return whether a durable run event with this exact action exists."""
        return (
            self.session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.run_id == run_id,
                    AuditEvent.action == action,
                )
                .limit(1)
            )
            is not None
        )

    def has_current_audit_action(self, run_id: str, action: str, version: int) -> bool:
        """Return whether an action belongs to the package version under review.

        New events identify their resulting version explicitly. Older events did
        not, so treat one as current only until a later artwork/package revision
        event proves that the operator moved on.
        """
        events = list(
            self.session.scalars(
                select(AuditEvent)
                .where(AuditEvent.run_id == run_id, AuditEvent.action == action)
                .order_by(AuditEvent.created_at.desc())
            )
        )
        if not events:
            return False
        event = events[0]
        event_version = event.detail.get("to_version", event.detail.get("version"))
        if event_version is not None:
            try:
                return int(event_version) == version
            except TypeError, ValueError:
                return False
        invalidating_actions = {
            "artwork.brief_rewritten",
            "artwork.regeneration_started",
            "artwork.safe_layout_fallback",
            "run.artwork_retried",
        }
        return (
            self.session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.run_id == run_id,
                    AuditEvent.created_at > event.created_at,
                    AuditEvent.action.in_(invalidating_actions),
                )
                .limit(1)
            )
            is None
        )

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        return list(
            self.session.scalars(
                select(RunRecord).order_by(RunRecord.created_at.desc()).limit(limit)
            )
        )

    def recent_concepts(self, exclude_run_id: str, days: int = 90) -> list[dict[str, Any]]:
        records = self.session.scalars(
            select(RunRecord)
            .where(
                RunRecord.id != exclude_run_id,
                RunRecord.created_at >= datetime.now(UTC) - timedelta(days=days),
                RunRecord.selected_concept.is_not(None),
            )
            .order_by(RunRecord.created_at.desc())
        )
        result: list[dict[str, Any]] = []
        for record in records:
            concept = record.selected_concept
            if not concept:
                continue
            result.append(
                {
                    "date": record.created_at.date().isoformat(),
                    "status": record.status,
                    "concept_name": concept.get("concept_name"),
                    "target_customer": concept.get("target_customer"),
                    "slogan": concept.get("slogan_if_any"),
                    "visual_concept": concept.get("visual_concept"),
                    "strategy": concept.get("strategy"),
                }
            )
            if len(result) == 30:
                break
        return result

    def view(self, record: RunRecord) -> RunView:
        return RunView(
            id=record.id,
            workflow_id=record.workflow_id,
            status=RunStatus(record.status),
            version=record.version,
            scheduled_for=record.scheduled_for,
            selected_concept=record.selected_concept,
            creative_brief=record.creative_brief,
            ip_report=record.ip_report,
            qa_report=record.qa_report,
            listings=record.listings,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def status(self, run_id: str, status: RunStatus, error: str | None = None) -> RunRecord:
        record = self.get(run_id)
        record.status = status.value
        record.error = error
        record.updated_at = datetime.now(UTC)
        self.audit(run_id, "worker", "run.status", {"status": status.value, "error": error})
        return record

    def provider_call(self, run_id: str, stage: str, metadata: dict[str, Any]) -> None:
        record = self.get(run_id)
        calls = list(record.provider_calls or [])
        calls.append({"stage": stage, **metadata})
        record.provider_calls = calls

    def store_research(self, run_id: str, report: dict[str, Any]) -> None:
        record = self.get(run_id)
        record.research_report = report
        self.session.query(ConceptRecord).filter(ConceptRecord.run_id == run_id).delete()
        for index, candidate in enumerate(report["candidates"], start=1):
            self.session.add(ConceptRecord(run_id=run_id, rank=index, data=candidate))

    def store_selection(
        self,
        run_id: str,
        decision: dict[str, Any],
        selected: CandidateConcept,
        eligibility: dict[str, tuple[bool, str | None, float]],
        ip_report: dict[str, Any] | None,
    ) -> None:
        record = self.get(run_id, full=True)
        record.selection = {
            **decision,
            "score_breakdowns": {
                concept.data["concept_name"]: concept_score_breakdown(
                    CandidateConcept.model_validate(concept.data),
                    include_ip_risk=ip_report is not None,
                )
                for concept in record.concepts
            },
        }
        record.selected_concept = selected.model_dump(mode="json")
        record.ip_report = ip_report
        for concept in record.concepts:
            eligible, reason, score = eligibility[concept.data["concept_name"]]
            concept.eligible = eligible
            concept.rejection_reason = reason
            concept.weighted_score = score
            concept.selected = concept.data["concept_name"] == selected.concept_name

    def store_opportunities(self, run_id: str, opportunities: list[ProductOpportunity]) -> None:
        record = self.get(run_id, full=True)
        record.pipeline_version = 2
        self.session.query(OpportunityRecord).filter(OpportunityRecord.run_id == run_id).delete()
        for rank, opportunity in enumerate(opportunities, start=1):
            self.session.add(
                OpportunityRecord(
                    run_id=run_id,
                    rank=rank,
                    weighted_score=opportunity.weighted_score,
                    eligible=opportunity.eligible,
                    rejection_reason=opportunity.rejection_reason,
                    data=opportunity.model_dump(mode="json"),
                )
            )

    def select_opportunity(self, run_id: str, opportunity: ProductOpportunity) -> None:
        record = self.get(run_id)
        prior_id = (record.selected_opportunity or {}).get("opportunity_id")
        if prior_id != opportunity.opportunity_id:
            record.reference_analysis = None
            record.product_plan = None
            record.originality_report = None
            record.seo_evidence = None
            record.price_decisions = None
            record.listings = None
            record.ip_report = None
            record.qa_report = None
        record.selected_opportunity = opportunity.model_dump(mode="json")
        for item in self.session.scalars(
            select(OpportunityRecord).where(OpportunityRecord.run_id == run_id)
        ):
            if item.data.get("opportunity_id") == opportunity.opportunity_id:
                item.eligible = True
            elif item.rejection_reason is None:
                item.rejection_reason = "lower ranked qualifying opportunity"

    def reject_opportunity(self, run_id: str, opportunity_id: str, reason: str) -> None:
        item = next(
            (
                candidate
                for candidate in self.session.scalars(
                    select(OpportunityRecord).where(OpportunityRecord.run_id == run_id)
                )
                if candidate.data.get("opportunity_id") == opportunity_id
            ),
            None,
        )
        if item is not None:
            item.eligible = False
            item.rejection_reason = reason
        record = self.get(run_id)
        if (record.selected_opportunity or {}).get("opportunity_id") == opportunity_id:
            record.selected_opportunity = None

    def store_v2_package(
        self,
        run_id: str,
        *,
        opportunity: ProductOpportunity,
        reference_analysis: ReferenceAnalysis,
        plan: ProductPlanV2,
        originality: OriginalityReport,
        seo: SEOEvidence,
        prices: list[PriceDecision],
        listings: dict[str, Any],
    ) -> None:
        record = self.get(run_id)
        record.pipeline_version = 2
        record.selected_opportunity = opportunity.model_dump(mode="json")
        record.reference_analysis = reference_analysis.model_dump(mode="json")
        record.product_plan = plan.model_dump(mode="json")
        record.originality_report = originality.model_dump(mode="json")
        record.seo_evidence = seo.model_dump(mode="json")
        record.price_decisions = [item.model_dump(mode="json") for item in prices]
        record.listings = listings

    def begin_revision(
        self, run_id: str, regenerate: bool, preserve_brief: bool = False
    ) -> RunRecord:
        record = self.get(run_id)
        if regenerate and record.status in {
            RunStatus.PENDING.value,
            RunStatus.AWAITING_APPROVAL.value,
        }:
            record.version += 1
            if not preserve_brief:
                record.creative_brief = None
            record.typography_spec = None
            record.qa_report = None
            record.listings = None
            record.listing_generation_state = None
            record.price_quotes = None
            record.template_snapshot = None
            record.excluded_shirt_colors = None
            record.publication_template_snapshot = None
            self.audit(run_id, "admin", "artwork.regeneration_started", {"version": record.version})
        return record

    def add_artifact(
        self,
        run_id: str,
        *,
        kind: str,
        revision: int,
        object_key: str,
        sha256: str,
        width: int,
        height: int,
        metadata: dict[str, Any],
    ) -> ArtifactRecord:
        record = ArtifactRecord(
            run_id=run_id,
            kind=kind,
            revision=revision,
            object_key=object_key,
            sha256=sha256,
            width=width,
            height=height,
            metadata_json=metadata,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def store_package(
        self,
        run_id: str,
        *,
        brief: dict[str, Any],
        typography: dict[str, Any] | None,
        qa: dict[str, Any],
        listings: dict[str, Any],
        quotes: list[dict[str, Any]],
        template: dict[str, Any],
        excluded_shirt_colors: list[str],
        publication_template: dict[str, Any],
    ) -> None:
        record = self.get(run_id)
        record.creative_brief = brief
        record.typography_spec = typography
        record.qa_report = qa
        record.listings = listings
        record.price_quotes = quotes
        record.template_snapshot = template
        record.excluded_shirt_colors = excluded_shirt_colors
        record.publication_template_snapshot = publication_template

    def update_package(
        self,
        run_id: str,
        *,
        expected_version: int,
        listings: dict[str, Any],
        quotes: list[dict[str, Any]],
        actor: str,
    ) -> RunRecord:
        record = self.get(run_id)
        if record.status != RunStatus.AWAITING_APPROVAL.value:
            raise ValueError("only a run awaiting approval can be edited")
        if record.version != expected_version:
            raise ValueError("review package version changed")
        record.version += 1
        record.listings = listings
        record.price_quotes = quotes
        self.audit(
            run_id,
            actor,
            "listing.package_edited",
            {"old_version": expected_version, "new_version": record.version},
        )
        return record

    def reprice_package(
        self,
        run_id: str,
        *,
        quotes: list[dict[str, Any]],
        reason: str,
    ) -> RunRecord:
        record = self.get(run_id)
        record.version += 1
        record.price_quotes = quotes
        record.status = RunStatus.AWAITING_APPROVAL.value
        record.error = reason
        self.audit(
            run_id,
            "worker",
            "approval.invalidated",
            {"reason": reason, "new_version": record.version},
        )
        return record

    def approve(self, run_id: str, signal: ApprovalSignal, decision: str = "approved") -> None:
        self.get(run_id)
        self.session.add(
            ApprovalRecord(
                run_id=run_id,
                version=signal.expected_version,
                actor=signal.actor,
                channels=[item.value for item in signal.channels],
                ip_attested=signal.ip_attested,
                decision=decision,
            )
        )
        self.audit(
            run_id,
            signal.actor,
            f"run.{decision}",
            {
                "version": signal.expected_version,
                "channels": [item.value for item in signal.channels],
            },
        )

    def publish_record(self, run_id: str, channel: str, fingerprint: str) -> PublishRecord:
        record = self.session.scalar(
            select(PublishRecord).where(
                PublishRecord.run_id == run_id, PublishRecord.channel == channel
            )
        )
        if record:
            if fingerprint != "pending":
                record.product_fingerprint = fingerprint
            return record
        record = PublishRecord(run_id=run_id, channel=channel, product_fingerprint=fingerprint)
        self.session.add(record)
        self.session.flush()
        return record

    def save_product_mapping(
        self,
        run_id: str,
        channel: str,
        printify_product_id: str,
        response: dict[str, Any],
    ) -> ProductMappingRecord:
        record = self.session.scalar(
            select(ProductMappingRecord).where(
                ProductMappingRecord.channel == channel,
                ProductMappingRecord.printify_product_id == printify_product_id,
            )
        )
        if record is None:
            selected = self.session.scalar(
                select(ConceptRecord).where(
                    ConceptRecord.run_id == run_id, ConceptRecord.selected.is_(True)
                )
            )
            record = ProductMappingRecord(
                run_id=run_id,
                concept_id=selected.id if selected else None,
                channel=channel,
                printify_product_id=printify_product_id,
            )
            self.session.add(record)
        external = response.get("external") or {}
        record.marketplace_product_id = external.get("id") or response.get("external_id")
        record.marketplace_listing_id = external.get("listing_id") or (
            record.marketplace_product_id if channel == "etsy" else None
        )
        record.asin = response.get("asin")
        record.skus = [
            str(item["sku"])
            for item in response.get("variants", [])
            if isinstance(item, dict) and item.get("sku")
        ]
        return record

    def audit(self, run_id: str | None, actor: str, action: str, detail: dict[str, Any]) -> None:
        self.session.add(AuditEvent(run_id=run_id, actor=actor, action=action, detail=detail))


class ConfigurationRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_template_record(self) -> ProductTemplateRecord:
        record = self.session.scalar(
            select(ProductTemplateRecord)
            .where(ProductTemplateRecord.active.is_(True))
            .order_by(ProductTemplateRecord.version.desc())
        )
        if record is None:
            raise RuntimeError("Product template must be configured before running the pipeline")
        return record

    def get_template(self) -> ProductTemplate:
        return ProductTemplate.model_validate(self.get_template_record().data)

    def activate_template(
        self, template: ProductTemplate, *, expected_version: int
    ) -> ProductTemplateRecord:
        current = lock_active_template(self.session)
        if current is None or current.version != expected_version:
            raise ValueError(
                "Active template changed during setup; preview the current catalog again"
            )
        if ProductTemplate.model_validate(current.data) == template:
            return current
        terminal = {
            RunStatus.PUBLISHED.value,
            RunStatus.REJECTED.value,
            RunStatus.CANCELLED.value,
            RunStatus.FAILED.value,
            RunStatus.NO_SAFE_CANDIDATE.value,
            RunStatus.NO_QUALIFIED_OPPORTUNITY.value,
        }
        active = self.session.scalar(
            select(RunRecord.id).where(RunRecord.status.not_in(terminal)).limit(1)
        )
        if active:
            raise ValueError(f"Resolve active run {active} before changing the template")
        record = self.save_template(template)
        RunRepository(self.session).audit(
            None, "admin", "template.activated", {"version": record.version, "name": template.name}
        )
        return record

    def save_template(self, template: ProductTemplate) -> ProductTemplateRecord:
        current = self.session.scalar(
            select(ProductTemplateRecord).order_by(ProductTemplateRecord.version.desc())
        )
        if current:
            current.active = False
            version = current.version + 1
        else:
            version = 1
        record = ProductTemplateRecord(
            id=version, data=template.model_dump(mode="json"), version=version, active=True
        )
        self.session.add(record)
        self.session.flush()
        return record


class CatalogRepository:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def product_key(blueprint_id: int, print_provider_id: int) -> str:
        return f"{blueprint_id}:{print_provider_id}"

    def upsert_product(self, product: CatalogProduct) -> CatalogProductRecord:
        key = self.product_key(product.blueprint_id, product.print_provider_id)
        record = self.session.get(CatalogProductRecord, key)
        if record is None:
            record = CatalogProductRecord(
                key=key,
                blueprint_id=product.blueprint_id,
                print_provider_id=product.print_provider_id,
                title=product.title,
                data=product.model_dump(mode="json"),
                source_fingerprint=product.source_fingerprint,
                synced_at=product.synced_at,
            )
            self.session.add(record)
        else:
            record.title = product.title
            record.data = product.model_dump(mode="json")
            record.source_fingerprint = product.source_fingerprint
            record.synced_at = product.synced_at
            self.session.query(CatalogVariantRecord).filter(
                CatalogVariantRecord.product_key == key
            ).delete()
        for variant in product.variants:
            self.session.add(
                CatalogVariantRecord(
                    id=f"{key}:{variant.variant_id}",
                    product_key=key,
                    variant_id=variant.variant_id,
                    available=variant.available,
                    data=variant.model_dump(mode="json"),
                    synced_at=product.synced_at,
                )
            )
        self.session.flush()
        return record

    def get(self, blueprint_id: int, print_provider_id: int) -> CatalogProduct:
        record = self.session.get(
            CatalogProductRecord, self.product_key(blueprint_id, print_provider_id)
        )
        if record is None:
            raise KeyError(f"catalog product {blueprint_id}/{print_provider_id} not found")
        return CatalogProduct.model_validate(record.data)

    def list_products(self) -> list[CatalogProduct]:
        return [
            CatalogProduct.model_validate(item.data)
            for item in self.session.scalars(
                select(CatalogProductRecord).order_by(
                    CatalogProductRecord.blueprint_id,
                    CatalogProductRecord.print_provider_id,
                )
            )
        ]

    def prune_products(self, active_keys: set[str]) -> int:
        if not active_keys:
            raise ValueError("refusing to prune the catalog without an active snapshot")
        stale = list(
            self.session.scalars(
                select(CatalogProductRecord).where(CatalogProductRecord.key.not_in(active_keys))
            )
        )
        for record in stale:
            self.session.delete(record)
        return len(stale)

    def observe_cost(
        self,
        *,
        account_plan: str,
        blueprint_id: int,
        print_provider_id: int,
        variant_id: int,
        cost_cents: int,
        source_fingerprint: str,
        evidence: dict[str, Any],
        observed_at: datetime,
    ) -> CostObservationRecord:
        record = CostObservationRecord(
            account_plan=account_plan,
            blueprint_id=blueprint_id,
            print_provider_id=print_provider_id,
            variant_id=variant_id,
            cost_cents=cost_cents,
            currency="USD",
            source_fingerprint=source_fingerprint,
            evidence=evidence,
            observed_at=observed_at,
        )
        self.session.add(record)
        self.session.flush()
        return record

    def latest_costs(
        self,
        blueprint_id: int,
        print_provider_id: int,
        *,
        observed_since: datetime,
    ) -> dict[int, CostObservationRecord]:
        records = self.session.scalars(
            select(CostObservationRecord)
            .where(
                CostObservationRecord.blueprint_id == blueprint_id,
                CostObservationRecord.print_provider_id == print_provider_id,
                CostObservationRecord.observed_at >= observed_since,
            )
            .order_by(CostObservationRecord.observed_at.desc())
        )
        latest: dict[int, CostObservationRecord] = {}
        for record in records:
            latest.setdefault(record.variant_id, record)
        return latest


class ResearchRepository:
    def __init__(self, session: Session):
        self.session = session

    def save_snapshot(self, snapshot: CompetitorListingSnapshot) -> CompetitorListingRecord:
        payload = snapshot.model_dump(mode="json")
        fingerprint = (
            __import__("hashlib").sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        )
        existing = self.session.scalar(
            select(CompetitorListingRecord).where(
                CompetitorListingRecord.fingerprint == fingerprint
            )
        )
        if existing is not None:
            return existing
        record = CompetitorListingRecord(
            marketplace=snapshot.marketplace.value,
            external_listing_id=snapshot.external_listing_id,
            url=snapshot.url,
            fingerprint=fingerprint,
            data=payload,
            collected_at=snapshot.collected_at,
        )
        self.session.add(record)
        self.session.flush()
        return record


class BrowserSessionRepository:
    def __init__(self, session: Session):
        self.session = session

    def save(
        self,
        source: str,
        encrypted_state: str,
        *,
        expires_at: datetime | None = None,
    ) -> BrowserSessionRecord:
        record = self.session.get(BrowserSessionRecord, source)
        if record is None:
            record = BrowserSessionRecord(source=source, encrypted_state=encrypted_state)
            self.session.add(record)
        else:
            record.encrypted_state = encrypted_state
        record.healthy = True
        record.detail = "connected"
        record.expires_at = expires_at
        record.updated_at = datetime.now(UTC)
        self.session.flush()
        return record

    def get(self, source: str) -> BrowserSessionRecord | None:
        return self.session.get(BrowserSessionRecord, source)

    def mark_unhealthy(self, source: str, detail: str) -> None:
        record = self.session.get(BrowserSessionRecord, source)
        if record is not None:
            record.healthy = False
            record.detail = detail[:2000]
            record.updated_at = datetime.now(UTC)


class MetricsRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, metric: DailyPerformance) -> None:
        if metric.concept_id is None and metric.external_product_id:
            mappings = self.session.scalars(
                select(ProductMappingRecord).where(
                    ProductMappingRecord.channel == metric.channel.value
                )
            )
            mapping = next(
                (
                    item
                    for item in mappings
                    if metric.external_product_id
                    in {
                        item.printify_product_id,
                        item.marketplace_product_id,
                        item.marketplace_listing_id,
                        item.asin,
                        *item.skus,
                    }
                ),
                None,
            )
            if mapping and mapping.concept_id:
                metric = metric.model_copy(update={"concept_id": UUID(mapping.concept_id)})
        identity = self.session.scalar(
            select(DailyMetricRecord).where(
                DailyMetricRecord.metric_date == metric.metric_date,
                DailyMetricRecord.channel == metric.channel.value,
                DailyMetricRecord.external_product_id == metric.external_product_id,
                DailyMetricRecord.source == metric.source,
            )
        )
        payload = metric.model_dump(mode="json")
        if identity:
            identity.data = payload
            identity.concept_id = (
                str(metric.concept_id) if metric.concept_id else identity.concept_id
            )
            identity.imported_at = datetime.now(UTC)
        else:
            self.session.add(
                DailyMetricRecord(
                    metric_date=metric.metric_date,
                    channel=metric.channel.value,
                    concept_id=str(metric.concept_id) if metric.concept_id else None,
                    external_product_id=metric.external_product_id,
                    data=payload,
                    source=metric.source,
                )
            )

    def recent(self, days: int = 90) -> list[DailyMetricRecord]:
        since = date.today() - timedelta(days=days)
        return list(
            self.session.scalars(
                select(DailyMetricRecord)
                .where(DailyMetricRecord.metric_date >= since)
                .order_by(DailyMetricRecord.metric_date.desc())
            )
        )

    def summary(self, days: int = 90) -> str:
        rows = self.recent(days)
        if not rows:
            return "No first-party performance data is available yet."
        # A one-time CSV import can predate its publication mapping. Resolve
        # those saved observations at read time without requiring a reimport.
        channels = {
            row.channel for row in rows if not (row.concept_id or row.data.get("concept_id"))
        }
        mapped_concepts: dict[tuple[str, str], set[str]] = {}
        if channels:
            mappings = self.session.scalars(
                select(ProductMappingRecord).where(ProductMappingRecord.channel.in_(channels))
            )
            for mapping in mappings:
                if not mapping.concept_id:
                    continue
                external_ids = {
                    mapping.printify_product_id,
                    mapping.marketplace_product_id,
                    mapping.marketplace_listing_id,
                    mapping.asin,
                    *(mapping.skus or []),
                }
                for external_id in external_ids:
                    if external_id:
                        mapped_concepts.setdefault(
                            (mapping.channel, str(external_id)),
                            set(),
                        ).add(mapping.concept_id)
        observations = []
        ids = set()
        for row in rows:
            concept_id = row.concept_id or row.data.get("concept_id")
            if not concept_id and row.external_product_id:
                matches = mapped_concepts.get((row.channel, row.external_product_id), set())
                if len(matches) == 1:
                    concept_id = next(iter(matches))
            if concept_id:
                ids.add(concept_id)
            observations.append(
                {
                    **row.data,
                    "concept_id": concept_id,
                    "channel": row.channel,
                    "source": row.source,
                    "external_product_id": row.external_product_id,
                    "metric_date": row.metric_date.isoformat(),
                }
            )
        concepts = {
            item.id: {
                "name": item.data.get("concept_name"),
                "strategy": item.data.get("strategy"),
                "target_customer": item.data.get("target_customer"),
            }
            for item in self.session.scalars(select(ConceptRecord).where(ConceptRecord.id.in_(ids)))
        }
        return json.dumps(
            summarize_performance(observations, concepts, days),
            sort_keys=True,
        )

    def connector_result(
        self, name: str, healthy: bool, detail: str, *, synced: bool = False
    ) -> None:
        now = datetime.now(UTC)
        record = self.session.get(ConnectorStateRecord, name)
        if record is None:
            record = ConnectorStateRecord(name=name)
            self.session.add(record)
        record.healthy = healthy
        record.detail = detail
        record.last_checked_at = now
        if synced:
            record.last_synced_at = now
