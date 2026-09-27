"""Persist Printify's exact product rank on synchronized catalog products."""

import sqlalchemy as sa

from alembic import op

revision = "0010_ranked_catalog_products"
down_revision = "0009_catalog_research_popularity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("catalog_products")}
    if "printify_rank" not in columns:
        op.add_column("catalog_products", sa.Column("printify_rank", sa.Integer()))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("catalog_products")}
    if "ix_catalog_products_printify_rank" not in indexes:
        op.create_index(
            "ix_catalog_products_printify_rank",
            "catalog_products",
            ["printify_rank"],
        )


def downgrade() -> None:
    op.drop_index("ix_catalog_products_printify_rank", table_name="catalog_products")
    op.drop_column("catalog_products", "printify_rank")
