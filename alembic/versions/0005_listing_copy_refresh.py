"""Checkpoint listing polish and reviewed updates to published copy."""

import sqlalchemy as sa

from alembic import op

revision = "0005_listing_copy_refresh"
down_revision = "0004_pub_template"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "listing_generation_state" not in {
        item["name"] for item in inspector.get_columns("workflow_runs")
    }:
        op.add_column(
            "workflow_runs",
            sa.Column("listing_generation_state", sa.JSON(), nullable=True),
        )
    tables = set(inspector.get_table_names())
    if "copy_refresh_batches" not in tables:
        op.create_table(
            "copy_refresh_batches",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("digest", sa.String(64)),
            sa.Column("approved_at", sa.DateTime(timezone=True)),
            sa.Column("approved_by", sa.String(128)),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_copy_refresh_batches_status", "copy_refresh_batches", ["status"])
    if "copy_refresh_items" not in tables:
        op.create_table(
            "copy_refresh_items",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "batch_id",
                sa.String(36),
                sa.ForeignKey("copy_refresh_batches.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_id",
                sa.String(36),
                sa.ForeignKey("workflow_runs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("channel", sa.String(32), nullable=False),
            sa.Column("printify_product_id", sa.String(128), nullable=False),
            sa.Column("marketplace_listing_id", sa.String(128), nullable=False),
            sa.Column("printify_shop_id", sa.String(128), nullable=False),
            sa.Column("before_json", sa.JSON()),
            sa.Column("after_json", sa.JSON()),
            sa.Column("baseline_json", sa.JSON()),
            sa.Column("generation_json", sa.JSON()),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("stage", sa.String(64)),
            sa.Column("error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("batch_id", "run_id", "channel", name="uq_copy_batch_run_channel"),
        )
        op.create_index("ix_copy_refresh_items_batch_id", "copy_refresh_items", ["batch_id"])
        op.create_index("ix_copy_refresh_items_run_id", "copy_refresh_items", ["run_id"])
        op.create_index("ix_copy_refresh_items_status", "copy_refresh_items", ["status"])


def downgrade() -> None:
    op.drop_table("copy_refresh_items")
    op.drop_table("copy_refresh_batches")
    op.drop_column("workflow_runs", "listing_generation_state")
