from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base — every model in app/models imports this."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a request-scoped DB session.

    Resets the two RLS session GUCs (see app/services/auth.py's
    _apply_rls_context) before returning the connection to the pool.
    They're set with plain SET, not SET LOCAL, so they survive a
    mid-request db.commit() — but that also means they'd otherwise leak
    into whichever request next checks out this same pooled connection,
    so every request clears them here regardless of whether it touched
    RLS at all. Committed explicitly so the reset itself isn't undone by
    close()'s own rollback of whatever transaction is still open."""
    db = SessionLocal()
    try:
        yield db
    finally:
        try:
            db.execute(text("RESET app.bypass_rls"))
            db.execute(text("RESET app.current_org_id"))
            db.commit()
        except Exception:
            db.rollback()
        db.close()
