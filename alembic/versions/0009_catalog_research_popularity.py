"""Add persistent popularity ranking to curated catalog products."""

import sqlalchemy as sa

from alembic import op

revision = "0009_catalog_research_popularity"
down_revision = "0008_catalog_curations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("catalog_curations")}
    if "research_priority" not in columns:
        op.add_column("catalog_curations", sa.Column("research_priority", sa.Integer()))
    if "popularity_reason" not in columns:
        op.add_column("catalog_curations", sa.Column("popularity_reason", sa.Text()))
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("catalog_curations")}
    if "ix_catalog_curations_research_priority" not in indexes:
        op.create_index(
            "ix_catalog_curations_research_priority",
            "catalog_curations",
            ["research_priority"],
        )


def downgrade() -> None:
    op.drop_index("ix_catalog_curations_research_priority", table_name="catalog_curations")
    op.drop_column("catalog_curations", "popularity_reason")
    op.drop_column("catalog_curations", "research_priority")
