"""Tipo de cambio SUNAT del día — para cotizar en SOLES cuando el cliente no maneja dólares.

El alquiler se cobra en USD, pero muchos clientes pagan en soles. Esta utilidad trae el TC
oficial SUNAT (fuente pública, sin token) y lo cachea en memoria (la tasa cambia 1 vez al día
hábil). Si la API falla → devuelve None y el bot sigue: cotiza en USD y dice que confirma el
monto en soles. Nunca rompe la respuesta.
"""
import time

import httpx

_TTL = 3 * 3600  # 3h: la tasa SUNAT se publica 1 vez al día
_cache = {"at": 0.0, "val": None}

# Fuente primaria: API pública del TC SUNAT (sin token). Fallback best-effort.
_PRIMARY = "https://api.apis.net.pe/v1/tipo-cambio-sunat"
_FALLBACK = "https://pe.dolarapi.com/v1/cotizaciones/sunat"


def _parse(d: dict) -> dict | None:
    try:
        compra = float(d.get("compra"))
        venta = float(d.get("venta"))
    except Exception:
        return None
    if compra <= 0 or venta <= 0:
        return None
    return {"compra": round(compra, 3), "venta": round(venta, 3),
            "fecha": d.get("fecha") or d.get("fechaActualizacion"), "origen": "SUNAT"}


async def get_sunat_rate() -> dict | None:
    """{'compra','venta','fecha','origen'} o None si no se pudo obtener. Cacheado 3h."""
    now = time.time()
    if _cache["val"] and now - _cache["at"] < _TTL:
        return _cache["val"]
    val = None
    try:
        async with httpx.AsyncClient(timeout=8) as h:
            for url in (_PRIMARY, _FALLBACK):
                try:
                    r = await h.get(url)
                    if r.status_code == 200:
                        val = _parse(r.json())
                        if val:
                            break
                except Exception:
                    continue
    except Exception:
        val = None
    if val:
        _cache["at"] = now
        _cache["val"] = val
    return val
