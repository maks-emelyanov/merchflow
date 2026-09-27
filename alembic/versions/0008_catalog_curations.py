"""Add persistent one-product-per-category catalog curation."""

import sqlalchemy as sa

from alembic import op

revision = "0008_catalog_curations"
down_revision = "0007_catalog_refreshes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "catalog_curations" in inspector.get_table_names():
        return
    op.create_table(
        "catalog_curations",
        sa.Column("category", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column(
            "product_key",
            sa.String(64),
            sa.ForeignKey("catalog_products.key", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("quality_score", sa.Integer(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("algorithm_version", sa.String(32), nullable=False),
        sa.Column("curated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_catalog_curations_product_key", "catalog_curations", ["product_key"])


def downgrade() -> None:
    op.drop_table("catalog_curations")
