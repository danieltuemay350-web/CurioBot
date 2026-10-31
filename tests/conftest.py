from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.database.models import Base
from app.database.session import make_engine


@pytest.fixture
def engine(tmp_path):
    db = tmp_path / "test.sqlite3"
    engine = make_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)
