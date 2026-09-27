"""Add resumable catalog refresh state and product availability metadata."""

import sqlalchemy as sa

from alembic import op

revision = "0007_catalog_refreshes"
down_revision = "0006_catalog_opportunities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "catalog_refreshes" not in tables:
        op.create_table(
            "catalog_refreshes",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("workflow_id", sa.String(128), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("active_lease", sa.String(32), unique=True),
            sa.Column("manifest", sa.JSON()),
            sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("blueprint_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("provider_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_pairs", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("refreshed_products", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("skipped_products", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("retired_products", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("warning_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("warning_samples", sa.JSON(), nullable=False),
            sa.Column("error", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_catalog_refreshes_status", "catalog_refreshes", ["status"])
        op.create_index(
            "ix_catalog_refreshes_workflow_id", "catalog_refreshes", ["workflow_id"]
        )

    columns = {item["name"] for item in inspector.get_columns("catalog_products")}
    additions = {
        "active": sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        "last_seen_refresh_id": sa.Column("last_seen_refresh_id", sa.String(36)),
        "last_checked_at": sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        "refresh_error": sa.Column("refresh_error", sa.Text()),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("catalog_products", column)
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("catalog_products")
    }
    desired_indexes = {
        "ix_catalog_products_active": ["active"],
        "ix_catalog_products_last_seen_refresh_id": ["last_seen_refresh_id"],
        "ix_catalog_products_last_checked_at": ["last_checked_at"],
    }
    for name, column_names in desired_indexes.items():
        if name not in indexes:
            op.create_index(name, "catalog_products", column_names)


def downgrade() -> None:
    indexes = {
        item["name"]
        for item in sa.inspect(op.get_bind()).get_indexes("catalog_products")
    }
    for name in (
        "ix_catalog_products_last_checked_at",
        "ix_catalog_products_last_seen_refresh_id",
        "ix_catalog_products_active",
    ):
        if name in indexes:
            op.drop_index(name, table_name="catalog_products")
    for column in ("refresh_error", "last_checked_at", "last_seen_refresh_id", "active"):
        op.drop_column("catalog_products", column)
    op.drop_table("catalog_refreshes")
