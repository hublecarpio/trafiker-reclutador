"""Higgsfield — motor de CREATIVOS del stack de trafiker.

Este módulo es donde se generan los creativos (imágenes y videos) que alimentan los
anuncios: el mismo material que luego rota en las campañas Click-to-WhatsApp (CTWA) y en
el resto del flujo de WhatsApp/Meta. Idea del stack:

    Higgsfield (genera el creativo)  →  anuncio CTWA en Meta  →  WhatsApp Cloud API
        →  Chatwoot  →  webhook  →  agente IA  →  [CALIFICA]/[HUMANO]  →  dashboard

Diseño:
- Lee `HIGGSFIELD_API_KEY` (+ `HIGGSFIELD_BASE_URL` opcional) desde `settings`/entorno.
- Si la key NO está configurada, NO revienta: devuelve `{"ok": False, "status":
  "no_configurado", ...}` para que el resto del sistema siga funcionando sin creativos.
- Dependencia única: `httpx` (ya lo usa el resto del proyecto).

⚠️ Los endpoints/campos exactos de la API de Higgsfield pueden variar según tu plan; este
adaptador deja el patrón listo (auth por Bearer, POST JSON, manejo de errores) para que
ajustes las rutas a tu cuenta sin tocar el resto del bot.
"""
from __future__ import annotations

import httpx

from .config import settings


def enabled() -> bool:
    """True si hay API key de Higgsfield configurada."""
    return bool(settings.higgsfield_api_key)


def _base_url() -> str:
    return (settings.higgsfield_base_url or "https://api.higgsfield.ai").rstrip("/")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.higgsfield_api_key}",
        "Content-Type": "application/json",
    }


def _not_configured() -> dict:
    """Resultado claro y estable cuando falta la key (no rompe el flujo)."""
    return {
        "ok": False,
        "status": "no_configurado",
        "error": "HIGGSFIELD_API_KEY no está configurada — no se generan creativos.",
    }


async def generate_image(
    prompt: str,
    *,
    width: int = 1024,
    height: int = 1024,
    count: int = 1,
    extra: dict | None = None,
    timeout: float = 120.0,
) -> dict:
    """Genera imágenes de anuncio a partir de un prompt de texto.

    Args:
        prompt: descripción del creativo (copy visual / dirección de arte).
        width, height: dimensiones del creativo (default 1024x1024).
        count: cuántas variaciones pedir (útil para A/B de creativos).
        extra: parámetros adicionales que quieras pasar tal cual a la API
            (ej. modelo, estilo, seed). Se mezclan sobre el payload base.
        timeout: segundos máximo de espera.

    Returns:
        dict con:
          - `{"ok": True, "status": "ok", "data": <respuesta_api>}` si generó.
          - `{"ok": False, "status": "no_configurado", ...}` si falta la key.
          - `{"ok": False, "status": "error", "error": "..."}` ante fallo HTTP/red.
    """
    if not enabled():
        return _not_configured()

    payload: dict = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "num_images": count,
    }
    if extra:
        payload.update(extra)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{_base_url()}/v1/images/generations",
                headers=_headers(),
                json=payload,
            )
        if r.status_code >= 400:
            return {"ok": False, "status": "error",
                    "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "status": "ok", "data": r.json()}
    except Exception as e:  # red caída, timeout, JSON inválido, etc.
        return {"ok": False, "status": "error", "error": str(e)[:300]}


async def generate_video(
    prompt: str,
    *,
    duration_seconds: int = 5,
    extra: dict | None = None,
    timeout: float = 300.0,
) -> dict:
    """Genera un clip de video para anuncios (stub listo para conectar).

    Misma forma de respuesta que `generate_image`. La generación de video suele ser
    asíncrona (devuelve un job id que luego consultas); aquí dejamos el POST inicial
    listo para que adaptes el polling a tu cuenta.
    """
    if not enabled():
        return _not_configured()

    payload: dict = {"prompt": prompt, "duration": duration_seconds}
    if extra:
        payload.update(extra)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(
                f"{_base_url()}/v1/videos/generations",
                headers=_headers(),
                json=payload,
            )
        if r.status_code >= 400:
            return {"ok": False, "status": "error",
                    "error": f"HTTP {r.status_code}: {r.text[:300]}"}
        return {"ok": True, "status": "ok", "data": r.json()}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)[:300]}
