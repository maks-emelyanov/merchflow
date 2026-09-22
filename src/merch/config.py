from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MERCH_",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["local", "test", "production"] = "local"
    provider_mode: Literal["fake", "live"] = "fake"
    publish_mode: Literal["dry_run", "live"] = "dry_run"
    pipeline_version: Literal[1, 2] = 2
    public_base_url: str = "http://localhost:8000"
    session_secret: SecretStr = SecretStr("local-only-change-me-please")
    admin_password_hash: SecretStr = SecretStr("")
    local_admin_password: SecretStr = SecretStr("merch-dev")
    credential_encryption_key: SecretStr = SecretStr("")

    database_url: str = "sqlite:///./merch.db"
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "merch"
    schedule_timezone: str = "America/New_York"
    workflow_hour: int = 9
    analytics_hour: int = 9
    schedule_minute: int = 30

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_path: Path = Path(".data/artifacts")
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "merch-artifacts"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")

    openai_api_key: SecretStr = SecretStr("")
    openai_text_model: str = "gpt-6-astra"
    openai_research_model: str = "gpt-5.6-terra"
    openai_listing_model: str = "gpt-5.6-terra"
    openai_typography_model: str = "gpt-5.6-terra"
    openai_image_model: str = "gpt-image-2.5-sunburst"
    openai_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    openai_creative_reasoning_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    openai_visual_qa_detail: Literal["low", "high", "original", "auto"] = "high"
    openai_image_quality: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    openai_image_revision_quality: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    flat_artwork_cleanup_enabled: bool = False
    max_revision_attempts: int = 3
    max_brief_rewrites: int = 8
    max_opportunity_attempts: int = 3
    etsy_native_publish_grace_seconds: int = 600

    browser_headless: bool = True
    browser_navigation_timeout_seconds: int = 30
    browser_max_concurrency_per_domain: int = 1
    competitor_direct_freshness_hours: int = 24
    competitor_fallback_freshness_hours: int = 72
    cost_freshness_hours: int = 24
    search_fallback_enabled: bool = True
    research_query_limit: int = 25
    research_listings_per_source: int = 2
    research_page_cap: int = 250
    comparison_postal_code: str = "10001"
    etsy_max_variations_supported: Literal[2, 3] = 2
    originality_min_score: int = 80
    originality_max_copying_risk: int = 20
    perceptual_hash_block_distance: int = 8

    printify_api_token: SecretStr = SecretStr("")
    printify_user_agent: str = "merch-pod/0.1"
    printify_webhook_secret: SecretStr = SecretStr("")
    printify_base_url: str = "https://api.printify.com/v1"
    printify_dashboard_base_url: str = "https://printify.com"
    printify_shop_shopify: str | None = None
    printify_shop_etsy: str | None = None
    printify_shop_amazon_us: str | None = None

    shopify_shop_domain: str | None = None
    shopify_admin_token: SecretStr = SecretStr("")
    shopify_api_version: str = "2026-07"
    etsy_api_key: SecretStr = SecretStr("")
    etsy_shared_secret: SecretStr = SecretStr("")
    etsy_access_token: SecretStr = SecretStr("")
    etsy_refresh_token: SecretStr = SecretStr("")
    etsy_shop_id: int | None = None
    amazon_lwa_client_id: SecretStr = SecretStr("")
    amazon_lwa_client_secret: SecretStr = SecretStr("")
    amazon_refresh_token: SecretStr = SecretStr("")
    amazon_marketplace_id: str = "ATVPDKIKX0DER"
    amazon_sp_api_url: str = "https://sellingpartnerapi-na.amazon.com"

    trusted_proxy_ips: str = "127.0.0.1,::1"
    login_attempts_per_15_minutes: int = 8
    otel_exporter_endpoint: str | None = None
    service_name: str = "merch-pod"

    realesrgan_binary: Path | None = None
    realesrgan_endpoint: str | None = None
    font_family: str = "Noto Sans"
    font_file: Path = Path("/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf")
    max_artifact_bytes: int = 100 * 1024 * 1024
    target_margin: float = 0.40
    etsy_percent_fee: float = 0.095
    etsy_fixed_fee_cents: int = 45
    configured_discount_cents: int = 0
    etsy_customer_shipping_cents: int = 0
    manual_approval_enabled: bool = False
    etsy_production_partner_check_enabled: bool = False
    ip_check_enabled: bool = False
    ip_risk_threshold: int = 20

    @field_validator("workflow_hour", "analytics_hour")
    @classmethod
    def valid_hour(cls, value: int) -> int:
        if not 0 <= value <= 23:
            raise ValueError("schedule hour must be between 0 and 23")
        return value

    @field_validator("schedule_minute")
    @classmethod
    def valid_schedule_minute(cls, value: int) -> int:
        if not 0 <= value <= 59:
            raise ValueError("schedule minute must be between 0 and 59")
        return value

    @field_validator("max_brief_rewrites")
    @classmethod
    def valid_brief_rewrites(cls, value: int) -> int:
        if not 0 <= value <= 32:
            raise ValueError("max brief rewrites must be between 0 and 32")
        return value

    @field_validator("max_opportunity_attempts")
    @classmethod
    def valid_opportunity_attempts(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("max opportunity attempts must be between 1 and 10")
        return value

    @field_validator("research_query_limit", "research_listings_per_source", "research_page_cap")
    @classmethod
    def positive_research_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("research limits must be positive")
        return value

    @field_validator(
        "browser_navigation_timeout_seconds",
        "competitor_direct_freshness_hours",
        "competitor_fallback_freshness_hours",
        "cost_freshness_hours",
    )
    @classmethod
    def positive_time_window(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("browser and freshness time windows must be positive")
        return value

    @field_validator("browser_max_concurrency_per_domain")
    @classmethod
    def bounded_browser_concurrency(cls, value: int) -> int:
        if not 1 <= value <= 4:
            raise ValueError("browser concurrency per domain must be between 1 and 4")
        return value

    @field_validator("originality_min_score", "originality_max_copying_risk")
    @classmethod
    def percentage_score(cls, value: int) -> int:
        if not 0 <= value <= 100:
            raise ValueError("originality thresholds must be between 0 and 100")
        return value

    @field_validator("perceptual_hash_block_distance")
    @classmethod
    def hash_distance(cls, value: int) -> int:
        if not 0 <= value <= 64:
            raise ValueError("perceptual hash distance must be between 0 and 64")
        return value

    @field_validator("etsy_native_publish_grace_seconds")
    @classmethod
    def valid_native_publish_grace(cls, value: int) -> int:
        if not 0 <= value <= 3600:
            raise ValueError("Etsy native publish grace must be between 0 and 3600 seconds")
        return value

    @model_validator(mode="after")
    def production_safety(self) -> Settings:
        if self.app_env == "production":
            if len(self.session_secret.get_secret_value()) < 32:
                raise ValueError("MERCH_SESSION_SECRET must be at least 32 characters")
            if not self.admin_password_hash.get_secret_value():
                raise ValueError("MERCH_ADMIN_PASSWORD_HASH is required in production")
            if not self.credential_encryption_key.get_secret_value():
                raise ValueError("MERCH_CREDENTIAL_ENCRYPTION_KEY is required in production")
            if self.provider_mode == "live" and not self.openai_api_key.get_secret_value():
                raise ValueError("MERCH_OPENAI_API_KEY is required for live providers")
        if not 0 < self.target_margin < 1:
            raise ValueError("target margin must be between zero and one")
        if not 0 <= self.etsy_percent_fee < 1:
            raise ValueError("Etsy percentage fee must be between zero and one")
        if min(
            self.etsy_fixed_fee_cents,
            self.configured_discount_cents,
            self.etsy_customer_shipping_cents,
        ) < 0:
            raise ValueError("Etsy fixed fee and configured discount cannot be negative")
        return self

    def printify_shop(self, channel: str) -> str | None:
        return {
            "shopify": self.printify_shop_shopify,
            "etsy": self.printify_shop_etsy,
            "amazon_us": self.printify_shop_amazon_us,
        }.get(channel)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
