from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base — every model in app/models imports this."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a request-scoped DB session.

    Deliberately does *not* touch the RLS session GUCs (see
    app/services/auth.py's _apply_rls_context) itself. _apply_rls_context
    uses SET LOCAL, which is transaction-scoped by construction — its
    effect can never survive past the transaction that set it, commit or
    rollback, so there's no cleanup for get_db() to do here even in
    principle: nothing set by a previous request/transaction can still be
    live by the time this connection is handed out again.

    That also means every route that commits mid-request (e.g.
    create-then-refresh) and then runs another RLS-relevant query must
    re-apply _apply_rls_context for that *next* transaction — see
    commit_with_rls_refresh in app/services/auth.py, which every such
    route uses instead of calling db.commit()/db.refresh() directly.

    Earlier versions of both this function and _apply_rls_context tried a
    session-level (non-LOCAL) SET instead, on the theory that it would
    survive a mid-request commit on its own. That held up under sequential
    curl testing but broke under real concurrent load: db.commit() can
    hand the Session a *different* pooled connection for its next
    transaction, and a session-level SET lives on the connection it was
    run on, not on the Session object — so the "still set" GUC was
    sometimes sitting on a connection nobody was using anymore. SET LOCAL
    plus explicit re-application after every commit doesn't have that
    failure mode.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
