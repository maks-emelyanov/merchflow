"""Add catalog-wide research and product planning state."""

import sqlalchemy as sa

from alembic import op

revision = "0006_catalog_opportunities"
down_revision = "0005_listing_copy_refresh"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("workflow_runs")}
    additions = {
        "pipeline_version": sa.Column(
            "pipeline_version", sa.Integer(), nullable=False, server_default="1"
        ),
        "selected_opportunity": sa.Column("selected_opportunity", sa.JSON()),
        "reference_analysis": sa.Column("reference_analysis", sa.JSON()),
        "product_plan": sa.Column("product_plan", sa.JSON()),
        "originality_report": sa.Column("originality_report", sa.JSON()),
        "seo_evidence": sa.Column("seo_evidence", sa.JSON()),
        "price_decisions": sa.Column("price_decisions", sa.JSON()),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("workflow_runs", column)

    tables = set(inspector.get_table_names())

    if "browser_sessions" not in tables:
        op.create_table(
            "browser_sessions",
            sa.Column("source", sa.String(64), primary_key=True),
            sa.Column("encrypted_state", sa.Text(), nullable=False),
            sa.Column("healthy", sa.Boolean(), nullable=False),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "catalog_products" not in tables:
        op.create_table(
            "catalog_products",
            sa.Column("key", sa.String(64), primary_key=True),
            sa.Column("blueprint_id", sa.Integer(), nullable=False),
            sa.Column("print_provider_id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("source_fingerprint", sa.String(64), nullable=False),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "blueprint_id", "print_provider_id", name="uq_catalog_blueprint_provider"
            ),
        )
        op.create_index("ix_catalog_products_blueprint_id", "catalog_products", ["blueprint_id"])
        op.create_index(
            "ix_catalog_products_print_provider_id", "catalog_products", ["print_provider_id"]
        )
        op.create_index(
            "ix_catalog_products_source_fingerprint", "catalog_products", ["source_fingerprint"]
        )
        op.create_index("ix_catalog_products_synced_at", "catalog_products", ["synced_at"])
    if "catalog_variants" not in tables:
        op.create_table(
            "catalog_variants",
            sa.Column("id", sa.String(96), primary_key=True),
            sa.Column(
                "product_key",
                sa.String(64),
                sa.ForeignKey("catalog_products.key", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("variant_id", sa.Integer(), nullable=False),
            sa.Column("available", sa.Boolean(), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("product_key", "variant_id", name="uq_catalog_product_variant"),
        )
        op.create_index("ix_catalog_variants_product_key", "catalog_variants", ["product_key"])
        op.create_index("ix_catalog_variants_variant_id", "catalog_variants", ["variant_id"])
        op.create_index("ix_catalog_variants_available", "catalog_variants", ["available"])
        op.create_index("ix_catalog_variants_synced_at", "catalog_variants", ["synced_at"])
    if "cost_observations" not in tables:
        op.create_table(
            "cost_observations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_plan", sa.String(64), nullable=False),
            sa.Column("blueprint_id", sa.Integer(), nullable=False),
            sa.Column("print_provider_id", sa.Integer(), nullable=False),
            sa.Column("variant_id", sa.Integer(), nullable=False),
            sa.Column("cost_cents", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(3), nullable=False),
            sa.Column("source_fingerprint", sa.String(64), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in (
            "account_plan",
            "blueprint_id",
            "print_provider_id",
            "variant_id",
            "source_fingerprint",
            "observed_at",
        ):
            op.create_index(f"ix_cost_observations_{column}", "cost_observations", [column])
    if "competitor_listing_snapshots" not in tables:
        op.create_table(
            "competitor_listing_snapshots",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("marketplace", sa.String(32), nullable=False),
            sa.Column("external_listing_id", sa.String(256), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        )
        for column in ("marketplace", "external_listing_id", "fingerprint", "collected_at"):
            op.create_index(
                f"ix_competitor_listing_snapshots_{column}",
                "competitor_listing_snapshots",
                [column],
            )
    if "product_opportunities" not in tables:
        op.create_table(
            "product_opportunities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "run_id",
                sa.String(36),
                sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("weighted_score", sa.Float(), nullable=False),
            sa.Column("eligible", sa.Boolean(), nullable=False),
            sa.Column("rejection_reason", sa.Text()),
            sa.Column("data", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "rank", name="uq_opportunity_run_rank"),
        )
        op.create_index("ix_product_opportunities_run_id", "product_opportunities", ["run_id"])


def downgrade() -> None:
    op.drop_table("product_opportunities")
    op.drop_table("competitor_listing_snapshots")
    op.drop_table("cost_observations")
    op.drop_table("catalog_variants")
    op.drop_table("catalog_products")
    op.drop_table("browser_sessions")
    for column in (
        "price_decisions",
        "seo_evidence",
        "originality_report",
        "product_plan",
        "selected_opportunity",
        "reference_analysis",
        "pipeline_version",
    ):
        op.drop_column("workflow_runs", column)
