import psycopg


async def ping_postgres(database_url: str, timeout_s: float) -> None:
    """Open a connection and run SELECT 1. Raises on any failure; returns nothing on success."""
    # connect_timeout is libpq's own limit on the connection attempt (whole seconds).
    # The caller also wraps this in asyncio.timeout, which bounds the *entire* call.
    async with await psycopg.AsyncConnection.connect(
        database_url, connect_timeout=int(timeout_s)
    ) as conn:
        await conn.execute("SELECT 1")