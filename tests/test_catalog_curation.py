from __future__ import annotations

from merch.catalog_curation import (
    classify_product,
    curate_synced_catalog,
    select_best_products,
)
from merch.database import get_engine, session_scope
from merch.domain.catalog import fixture_catalog
from merch.models import Base
from merch.repository import CatalogRepository


def test_category_rules_prefer_specific_product_types() -> None:
    source = fixture_catalog()[0]
    products = [
        source.model_copy(update={"title": "Kids Pullover Hoodie"}),
        source.model_copy(update={"title": "Insulated Travel Mug"}),
        source.model_copy(update={"title": "Tough Phone Case"}),
    ]
    assert [classify_product(item).slug for item in products] == [  # type: ignore[union-attr]
        "kids-apparel",
        "water-bottle-tumbler",
        "phone-case",
    ]


def test_reviewed_premium_model_wins_its_category() -> None:
    source = fixture_catalog()[0]
    reviewed = source.model_copy(
        update={
            "blueprint_id": 706,
            "print_provider_id": 1,
            "title": "Unisex Garment-Dyed T-Shirt",
            "brand": "Comfort Colors",
            "model": "1717",
        }
    )
    generic_choice = source.model_copy(
        update={
            "blueprint_id": 999,
            "print_provider_id": 99,
            "title": "Unisex Premium T-Shirt",
            "brand": "Generic brand",
        }
    )
    result = select_best_products([generic_choice, reviewed])
    assert len(result) == 1
    assert result[0].product.blueprint_id == 706
    assert "reviewed premium model" in result[0].reasons[0]


def test_applied_curation_keeps_source_catalog_but_limits_effective_options(
    isolated_app,
) -> None:
    Base.metadata.create_all(get_engine())
    fixtures = fixture_catalog()
    pet_product = fixtures[0].model_copy(
        update={
            "blueprint_id": 99001,
            "print_provider_id": 99,
            "title": "Premium Pet Bowl",
        }
    )
    products = [*fixtures, pet_product]
    with session_scope() as session:
        repository = CatalogRepository(session)
        for rank, product in enumerate(fixtures, start=1):
            repository.upsert_product(product, printify_rank=rank)
        repository.upsert_product(pet_product)

    result = curate_synced_catalog(apply=True)

    assert result["applied"] is True
    assert result["source_products"] == len(fixtures)
    assert result["categories"] == len(fixtures)
    assert result["popular_categories"] == len(fixtures)
    with session_scope() as session:
        repository = CatalogRepository(session)
        assert len(repository.list_products()) == len(products)
        assert len(repository.list_effective_products()) == len(fixtures)
        rows = repository.curation_rows()
        assert len(rows) == len(fixtures)
        research_products = repository.list_research_products()
        assert len(research_products) == len(fixtures)
        assert pet_product.blueprint_id not in {item.blueprint_id for item in research_products}
        priorities = [
            row["research_priority"]
            for row in sorted(rows, key=lambda row: row["research_priority"])
        ]
        assert priorities == sorted(priorities)


def test_research_eligibility_requires_an_exact_ranked_blueprint(isolated_app) -> None:
    Base.metadata.create_all(get_engine())
    ranked, unranked = fixture_catalog()[:2]
    with session_scope() as session:
        repository = CatalogRepository(session)
        repository.upsert_product(ranked, printify_rank=7)
        repository.upsert_product(unranked)
        assert [item.blueprint_id for item in repository.list_research_products()] == [
            ranked.blueprint_id
        ]
