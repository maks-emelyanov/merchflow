from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from merch.schemas import PublishStatus, RunStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    research_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    selection: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    selected_concept: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    creative_brief: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    typography_spec: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    ip_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    qa_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    listings: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    price_quotes: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    template_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    excluded_shirt_colors: Mapped[list[str] | None] = mapped_column(JSON)
    publication_template_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    listing_generation_state: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    pipeline_version: Mapped[int] = mapped_column(Integer, default=1)
    selected_opportunity: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reference_analysis: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    product_plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    originality_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    seo_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    price_decisions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    provider_calls: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    concepts: Mapped[list[ConceptRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[ArtifactRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    approvals: Mapped[list[ApprovalRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    publishes: Mapped[list[PublishRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    opportunities: Mapped[list[OpportunityRecord]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ConceptRecord(Base):
    __tablename__ = "concepts"
    __table_args__ = (UniqueConstraint("run_id", "rank", name="uq_concept_run_rank"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    weighted_score: Mapped[float | None] = mapped_column(Float)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    run: Mapped[RunRecord] = relationship(back_populates="concepts")


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "kind", "revision", name="uq_artifact_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    content_type: Mapped[str] = mapped_column(String(100), default="image/png")
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run: Mapped[RunRecord] = relationship(back_populates="artifacts")


class ApprovalRecord(Base):
    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(128))
    channels: Mapped[list[str]] = mapped_column(JSON)
    ip_attested: Mapped[bool] = mapped_column(Boolean)
    decision: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run: Mapped[RunRecord] = relationship(back_populates="approvals")


class PublishRecord(Base):
    __tablename__ = "publishes"
    __table_args__ = (UniqueConstraint("run_id", "channel", name="uq_publish_run_channel"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(40), default=PublishStatus.PENDING.value)
    artwork_upload_id: Mapped[str | None] = mapped_column(String(128))
    printify_product_id: Mapped[str | None] = mapped_column(String(128))
    external_product_id: Mapped[str | None] = mapped_column(String(128))
    product_fingerprint: Mapped[str] = mapped_column(String(64))
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    run: Mapped[RunRecord] = relationship(back_populates="publishes")


class ProductMappingRecord(Base):
    __tablename__ = "product_mappings"
    __table_args__ = (
        UniqueConstraint("channel", "printify_product_id", name="uq_mapping_printify_channel"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    concept_id: Mapped[str | None] = mapped_column(String(36), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    printify_product_id: Mapped[str] = mapped_column(String(128), index=True)
    marketplace_product_id: Mapped[str | None] = mapped_column(String(128), index=True)
    marketplace_listing_id: Mapped[str | None] = mapped_column(String(128), index=True)
    asin: Mapped[str | None] = mapped_column(String(32), index=True)
    skus: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CopyRefreshBatchRecord(Base):
    __tablename__ = "copy_refresh_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    status: Mapped[str] = mapped_column(String(32), default="preparing", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    digest: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(128))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    items: Mapped[list[CopyRefreshItemRecord]] = relationship(
        back_populates="batch", cascade="all, delete-orphan"
    )


class CopyRefreshItemRecord(Base):
    __tablename__ = "copy_refresh_items"
    __table_args__ = (UniqueConstraint("batch_id", "run_id", "channel", name="uq_copy_batch_run_channel"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    batch_id: Mapped[str] = mapped_column(ForeignKey("copy_refresh_batches.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    printify_product_id: Mapped[str] = mapped_column(String(128))
    marketplace_listing_id: Mapped[str] = mapped_column(String(128))
    printify_shop_id: Mapped[str] = mapped_column(String(128))
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    baseline_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    generation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="preparing", index=True)
    stage: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    batch: Mapped[CopyRefreshBatchRecord] = relationship(back_populates="items")


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("channel", "external_order_id", name="uq_external_order"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    channel: Mapped[str] = mapped_column(String(32), index=True)
    external_order_id: Mapped[str] = mapped_column(String(128), index=True)
    printify_product_id: Mapped[str | None] = mapped_column(String(128), index=True)
    marketplace_product_id: Mapped[str | None] = mapped_column(String(128), index=True)
    sku: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    gross_cents: Mapped[int | None] = mapped_column(Integer)
    fulfillment_cost_cents: Mapped[int | None] = mapped_column(Integer)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CsvImportRecord(Base):
    __tablename__ = "csv_imports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    channel: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    rows_imported: Mapped[int] = mapped_column(Integer)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(128))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectorCredentialRecord(Base):
    __tablename__ = "connector_credentials"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BrowserSessionRecord(Base):
    __tablename__ = "browser_sessions"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    encrypted_state: Mapped[str] = mapped_column(Text)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str] = mapped_column(Text, default="connected")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class CatalogProductRecord(Base):
    __tablename__ = "catalog_products"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    blueprint_id: Mapped[int] = mapped_column(Integer, index=True)
    print_provider_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(512))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    variants: Mapped[list[CatalogVariantRecord]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint(
            "blueprint_id", "print_provider_id", name="uq_catalog_blueprint_provider"
        ),
    )


class CatalogVariantRecord(Base):
    __tablename__ = "catalog_variants"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    product_key: Mapped[str] = mapped_column(
        ForeignKey("catalog_products.key", ondelete="CASCADE"), index=True
    )
    variant_id: Mapped[int] = mapped_column(Integer, index=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    product: Mapped[CatalogProductRecord] = relationship(back_populates="variants")

    __table_args__ = (
        UniqueConstraint("product_key", "variant_id", name="uq_catalog_product_variant"),
    )


class CostObservationRecord(Base):
    __tablename__ = "cost_observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    account_plan: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    blueprint_id: Mapped[int] = mapped_column(Integer, index=True)
    print_provider_id: Mapped[int] = mapped_column(Integer, index=True)
    variant_id: Mapped[int] = mapped_column(Integer, index=True)
    cost_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class CompetitorListingRecord(Base):
    __tablename__ = "competitor_listing_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    marketplace: Mapped[str] = mapped_column(String(32), index=True)
    external_listing_id: Mapped[str] = mapped_column(String(256), index=True)
    url: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class OpportunityRecord(Base):
    __tablename__ = "product_opportunities"
    __table_args__ = (UniqueConstraint("run_id", "rank", name="uq_opportunity_run_rank"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    weighted_score: Mapped[float] = mapped_column(Float)
    eligible: Mapped[bool] = mapped_column(Boolean, default=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    run: Mapped[RunRecord] = relationship(back_populates="opportunities")


class ProductTemplateRecord(Base):
    __tablename__ = "product_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyMetricRecord(Base):
    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint(
            "metric_date", "channel", "external_product_id", "source", name="uq_daily_metric"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    concept_id: Mapped[str | None] = mapped_column(String(36), index=True)
    external_product_id: Mapped[str | None] = mapped_column(String(128), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConnectorStateRecord(Base):
    __tablename__ = "connector_states"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str] = mapped_column(Text, default="not checked")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
