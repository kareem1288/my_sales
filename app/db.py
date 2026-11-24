from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
	pass


def _make_engine():
	url = get_settings().database_url
	if url.startswith("sqlite"):
		return create_engine(url, connect_args={"check_same_thread": False})
	# Serverless functions should not hold large pools open.
	return create_engine(url, pool_pre_ping=True, pool_size=2, max_overflow=3, pool_recycle=300)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
	from . import models  # noqa: F401 - register tables

	Base.metadata.create_all(engine)


@contextmanager
def session_scope():
	db = SessionLocal()
	try:
		yield db
		db.commit()
	except Exception:
		db.rollback()
		raise
	finally:
		db.close()
