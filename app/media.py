"""Capa de entendimiento de media: convierte CUALQUIER input a TEXTO con ETIQUETA,
para que el agente (que solo lee texto) sepa qué llegó y pueda evaluar su contenido.

- Audio  -> Whisper (OpenAI)      -> transcripción
- Imagen -> Gemini Vision         -> descripción (ej. fotos/documentos que envía el lead)
- Video/otros -> etiqueta de tipo (sin procesar)

Cada función falla suave (devuelve None / etiqueta básica) si no hay key o hay error.
"""
import base64
import logging
import mimetypes
import os

import httpx

from .config import settings

log = logging.getLogger("recruitbot.media")


async def transcribe_audio(path: str) -> str | None:
    """Transcribe un audio con Whisper (OpenAI). Devuelve el texto o None."""
    if not settings.openai_api_key or not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
        async with httpx.AsyncClient(timeout=120) as cl:
            r = await cl.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                files={"file": ("audio.ogg", data, "audio/ogg")},
                data={"model": "whisper-1"})
        if r.status_code >= 400:
            log.warning("whisper %s: %s", r.status_code, r.text[:200])
            return None
        return (r.json().get("text") or "").strip() or None
    except Exception:
        log.exception("transcribe_audio falló")
        return None


async def describe_image(path: str, file_type: str | None = None) -> str | None:
    """Describe una imagen con Gemini Vision. Devuelve la descripción o None."""
    if not settings.gemini_api_key or not path or not os.path.isfile(path):
        return None
    mime = (file_type if (file_type or "").startswith("image/") else None) \
        or mimetypes.guess_type(path)[0] or "image/jpeg"
    prompt = ("Describe en español, en 1-3 frases, qué se ve en esta imagen. "
              "Si es un documento (DNI, CV, comprobante) di qué documento es y los datos clave visibles. "
              "Si es un producto u objeto, describe tipo, color y estado visibles.")
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"gemini-2.5-flash:generateContent?key={settings.gemini_api_key}")
        body = {"contents": [{"parts": [
            {"text": prompt}, {"inline_data": {"mime_type": mime, "data": b64}}]}]}
        async with httpx.AsyncClient(timeout=60) as cl:
            r = await cl.post(url, json=body)
        if r.status_code >= 400:
            log.warning("gemini %s: %s", r.status_code, r.text[:200])
            return None
        return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip() or None
    except Exception:
        log.exception("describe_image falló")
        return None


async def to_label(local_path: str, file_type: str | None, file_name: str = "") -> str:
    """Texto ETIQUETADO para inyectar en el historial del agente (siempre devuelve algo)."""
    ft = (file_type or "").lower()
    if "audio" in ft:
        t = await transcribe_audio(local_path)
        return f"🎙️ Audio del candidato (transcripción): {t}" if t \
            else "🎙️ Audio del candidato (recibido, no se pudo transcribir)"
    if "image" in ft:
        d = await describe_image(local_path, file_type)
        return f"🖼️ Imagen del candidato: {d}" if d else "🖼️ Imagen del candidato (recibida)"
    if "video" in ft:
        return "🎬 Video del candidato (recibido)"
    return f"📎 Documento del candidato: {file_name or 'archivo'} (recibido)"
