"""Cliente Evolution API (WhatsApp Web/Baileys) sobre el WhatsApp PERSONAL del dueño.

Se usa para RECONTACTAR candidatos que quedaron FUERA de la ventana de 24h del número
oficial (Cloud API), sin gastar plantillas: el "asistente reclutador" escribe desde el
WhatsApp personal del dueño y este confirma humanamente desde su perfil.

Nota: Evolution/Baileys es WhatsApp NO oficial — úsese con mesura (riesgo de baneo del
número si se envía masivo/spam). Por eso es para goteo de recontactos puntuales.
"""
import base64
import mimetypes
import os

import httpx

from .config import settings


def _base() -> str:
    return (settings.evolution_url or "").rstrip("/")


def enabled() -> bool:
    return bool(settings.evolution_url and settings.evolution_key and settings.evolution_instance)


def _digits(phone: str) -> str:
    return "".join(ch for ch in (phone or "") if ch.isdigit())


async def connection_state() -> str | None:
    """Devuelve el estado de la instancia ('open' = conectada) o None si falla."""
    if not enabled():
        return None
    url = f"{_base()}/instance/connectionState/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(url, headers={"apikey": settings.evolution_key})
            return (r.json().get("instance") or {}).get("state")
    except Exception:
        return None


async def send_text(phone: str, text: str) -> dict | None:
    """Envía un texto desde el WhatsApp personal vía Evolution. Devuelve la respuesta
    de la API (con la key del mensaje) o {'error': ...} / None si falla."""
    if not enabled():
        return None
    to = _digits(phone)
    if not to:
        return None
    url = f"{_base()}/message/sendText/{settings.evolution_instance}"
    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.post(url, headers={"apikey": settings.evolution_key,
                                            "Content-Type": "application/json"},
                              json={"number": to, "text": text})
            data = r.json()
            if r.status_code >= 400 or (isinstance(data, dict) and data.get("error")):
                return {"error": data}
            return data
    except Exception as e:
        return {"error": str(e)}


async def send_document(phone: str, path: str, filename: str | None = None) -> dict | None:
    """Envía un documento (CV, audio, etc.) desde el WhatsApp personal vía Evolution."""
    if not enabled() or not path or not os.path.isfile(path):
        return None
    to = _digits(phone)
    fn = filename or os.path.basename(path)
    mt = mimetypes.guess_type(path)[0] or "application/octet-stream"
    kind = "audio" if mt.startswith("audio") else "video" if mt.startswith("video") else \
           "image" if mt.startswith("image") else "document"
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post(f"{_base()}/message/sendMedia/{settings.evolution_instance}",
                              headers={"apikey": settings.evolution_key, "Content-Type": "application/json"},
                              json={"number": to, "mediatype": kind, "mimetype": mt, "media": b64, "fileName": fn})
            return {"status": r.status_code}
    except Exception as e:
        return {"error": str(e)}
