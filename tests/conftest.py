from __future__ import annotations

import pytest

from almali_library_curator.config import Settings


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(config_dir=tmp_path / "config")
