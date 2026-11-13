"""Human-friendly document numbers: INV-2026-0042, CTR-2026-0007.

The counter is a Postgres sequence, not "max(number) + 1": invoices are
also created by clients (milestone approval), whose queries only see their
own organization's rows under row-level security — counting would hand out
duplicates. Sequences ignore RLS and never repeat a value.

SQLite (the test suite) has no sequences, so there the next number comes
from counting rows, which is fine for a single-connection test database.
"""

from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection


def next_document_number(connection: Connection, table, column, sequence: str, prefix: str) -> str:
    year = datetime.now(timezone.utc).year
    if connection.dialect.name == "postgresql":
        n = connection.execute(text(f"SELECT nextval('{sequence}')")).scalar_one()
    else:
        n = (connection.execute(select(func.count()).select_from(table).where(column.isnot(None))).scalar_one() or 0) + 1
    return f"{prefix}-{year}-{n:04d}"
