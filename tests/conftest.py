from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from merch.config import get_settings
from merch.database import get_engine, get_session_factory


@pytest.fixture(autouse=True)
def fake_providers_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI and local tests must not call paid APIs when .env enables live providers."""
    monkeypatch.setenv("MERCH_PROVIDER_MODE", "fake")
    monkeypatch.setenv("MERCH_PUBLISH_MODE", "dry_run")
    monkeypatch.setenv("MERCH_MANUAL_APPROVAL_ENABLED", "false")
    monkeypatch.setenv("MERCH_ETSY_PRODUCTION_PARTNER_CHECK_ENABLED", "false")
    monkeypatch.setenv("MERCH_IP_CHECK_ENABLED", "false")
    # The production image installs the full Noto/Roboto registry.  Keep the
    # host-side fixture pipeline deterministic on lean developer images too,
    # without weakening production's fail-closed font resolution.
    fallback_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if fallback_font.is_file():
        monkeypatch.setenv("MERCH_FONT_FAMILY", "DejaVu Sans")
        monkeypatch.setenv("MERCH_FONT_FILE", str(fallback_font))


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    database = tmp_path / "test.db"
    monkeypatch.setenv("MERCH_APP_ENV", "test")
    monkeypatch.setenv("MERCH_PROVIDER_MODE", "fake")
    monkeypatch.setenv("MERCH_PUBLISH_MODE", "dry_run")
    monkeypatch.setenv("MERCH_DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("MERCH_LOCAL_STORAGE_PATH", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MERCH_LOCAL_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("MERCH_SESSION_SECRET", "test-session-secret-that-is-long-enough")
    get_settings.cache_clear()
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    yield tmp_path
    get_session_factory.cache_clear()
    get_engine.cache_clear()
    get_settings.cache_clear()
