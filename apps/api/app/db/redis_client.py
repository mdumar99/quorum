from redis.asyncio import Redis


async def ping_redis(redis_url: str, timeout_s: float) -> None:
    """Send PING. Raises on any failure; returns nothing on success."""
    client = Redis.from_url(redis_url, socket_connect_timeout=timeout_s, socket_timeout=timeout_s)
    try:
        await client.ping()
    finally:
        await client.aclose()  # always release the connection, success or failure
