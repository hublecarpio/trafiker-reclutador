"""Override de configuración por-agente desde Postgres (tabla `agents`).

Permite que el system_prompt y el modelo vivan en la DB (editables desde el panel)
en vez de hardcodeados en roles.py. SIEMPRE con fallback: si la fila no existe o la
DB falla, el caller usa `build_system_prompt(role)` de siempre → nunca rompe el bot.
Cache de 60s para no pegarle a la DB en cada mensaje.
"""
import json
import time
import asyncpg

from .config import settings

_TTL = 60
_cache: dict[str, tuple[float, dict | None]] = {}


async def get_agent_override(slug: str) -> dict | None:
    """Devuelve {'system_prompt', 'model'} para el agente `slug` (si está activo),
    o None si no hay fila / la DB falla (→ el caller hace fallback a roles.py)."""
    now = time.time()
    hit = _cache.get(slug)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        conn = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
        try:
            row = await conn.fetchrow(
                "SELECT system_prompt, model, tools FROM agents WHERE slug=$1 AND active", slug)
        finally:
            await conn.close()
        if row:
            raw = row["tools"]
            tools = json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
            val = {"system_prompt": row["system_prompt"], "model": row["model"], "tools": tools}
        else:
            val = None
        _cache[slug] = (now, val)   # cachea solo en éxito (incluido 'no hay fila')
        return val
    except Exception:
        return None  # error transitorio → sin cache, reintenta luego; caller usa fallback
