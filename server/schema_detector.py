import time
import logging
from contextlib import nullcontext
from server.db import pool, DB_SCHEMA

logger = logging.getLogger(__name__)

_cache: dict[str, set[str]] = {}
_cache_ts: float = 0
CACHE_TTL = 30


def _refresh_cache(connection=None):
    global _cache, _cache_ts
    # Reuse a caller's transaction instead of acquiring a second pool slot.
    with (nullcontext(connection) if connection is not None else pool.connection()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name "
                "FROM information_schema.columns "
                "WHERE table_schema = %s "
                "ORDER BY table_name, ordinal_position",
                (DB_SCHEMA,),
            )
            tables: dict[str, set[str]] = {}
            for table_name, column_name in cur.fetchall():
                tables.setdefault(table_name, set()).add(column_name)
            _cache = tables
            _cache_ts = time.monotonic()


def get_schema(connection=None) -> dict[str, set[str]]:
    if CACHE_TTL <= 0 or not _cache_ts or time.monotonic() - _cache_ts >= CACHE_TTL:
        try:
            _refresh_cache(connection)
        except Exception as e:
            logger.warning(f"Schema detection failed: {e}")
            if _cache:
                return _cache
            raise
    return _cache


def invalidate_cache():
    global _cache_ts
    _cache_ts = 0


def table_exists(table: str, connection=None) -> bool:
    return table in get_schema(connection)


def column_exists(table: str, column: str, connection=None) -> bool:
    schema = get_schema(connection)
    return table in schema and column in schema[table]


def get_features() -> dict:
    return {
        "reviews_active": table_exists("reviews"),
        "loyalty_active": table_exists("loyalty_members") and column_exists("customers", "loyalty_points"),
        "exchange_rates_active": table_exists("exchange_rates"),
        "order_priority_active": column_exists("orders", "priority") if table_exists("orders") else False,
        "email_verified_active": column_exists("customers", "email_verified"),
        "orders_available": table_exists("orders"),
        "order_items_available": table_exists("order_items"),
        "promotions_active": get_promotions_table() is not None,
        "demand_active": get_demand_table() is not None,
    }


def get_promotions_table(connection=None) -> str | None:
    """Return the actual promotions table name.

    Prefers the canonical names (`promotions_synced_prod`, then `promotions`),
    but falls back to any table whose name contains "promotions" so the
    storefront still works if the synced table was named slightly differently.
    """
    if table_exists("promotions_synced_prod", connection):
        return "promotions_synced_prod"
    if table_exists("promotions", connection):
        return "promotions"
    matches = sorted(t for t in get_schema(connection) if "promotions" in t.lower())
    return matches[0] if matches else None


def get_demand_table() -> str | None:
    """Return an optional externally maintained product-demand table name, or None.

    Prefers the canonical synced names, then falls back to any table whose name
    contains "product_demand" — so the Supplier View lights up regardless of the
    exact synced-table name chosen when the sync was created.
    """
    for name in ("product_demand_synced_prod", "product_demand_synced", "product_demand"):
        if table_exists(name):
            return name
    matches = sorted(t for t in get_schema() if "product_demand" in t.lower())
    return matches[0] if matches else None
