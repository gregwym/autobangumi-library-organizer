import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

source = Path(os.environ["AB_SOURCE"]).resolve() / "backend" / "src"
sys.path.insert(0, str(source))
# Production gets this generated module from the official image build.
sys.modules["module.__version__"] = SimpleNamespace(VERSION="4.0.0")


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    from module import conf
    from module.database import combine
    from module.downloader.download_client import _reset_client_cache
    from module.models.config import Config
    from module.plugin import host
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from sqlmodel import SQLModel, create_engine

    # Imports register AB's real schema; each test gets an isolated file DB.
    engine = create_engine(f"sqlite:///{tmp_path / 'host.db'}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}", poolclass=NullPool
    )
    monkeypatch.setattr(
        combine, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False)
    )
    defaults = Config()
    for key in Config.model_fields:
        monkeypatch.setattr(conf.settings, key, getattr(defaults, key))
    conf.settings.rss_parser.engine = "tokenizer"
    monkeypatch.setattr(host, "_registry", None)
    monkeypatch.setattr(host, "_runner", None)
    _reset_client_cache()
    yield
    _reset_client_cache()
