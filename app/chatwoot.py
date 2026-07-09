"""Thin Chatwoot API client — send outgoing messages, download attachments."""
import os
import httpx

from .config import settings


def _headers():
    return {"api_access_token": settings.chatwoot_token, "Content-Type": "application/json"}


def _whatsapp_safe(path: str) -> str:
    """WhatsApp rechaza imágenes > 5MB. Si es una imagen pesada, la recomprime a JPEG
    liviano (<=1600px, q88) en /tmp y devuelve esa ruta. Otros archivos se mandan tal cual."""
    try:
        if not os.path.isfile(path):
            return path
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return path
        if os.path.getsize(path) <= 4_500_000:
            return path
        from PIL import Image
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if max(w, h) > 1600:
            s = 1600 / max(w, h)
            im = im.resize((int(w * s), int(h * s)))
        out = os.path.join("/tmp", "wa_" + os.path.splitext(os.path.basename(path))[0] + ".jpg")
        im.save(out, "JPEG", quality=88, optimize=True)
        return out
    except Exception:
        return path


# cache de las etiquetas de CUENTA ya existentes (para no crearlas en cada mensaje)
_account_labels_cache: set[str] = set()
# paleta para asignar color a etiquetas nuevas de cuenta
_LABEL_COLORS = ["#1f93ff", "#9c27b0", "#ff9800", "#4caf50", "#e91e63", "#00bcd4",
                 "#3f51b5", "#795548", "#009688", "#607d8b", "#8bc34a", "#673ab7"]


async def _ensure_account_labels(client: httpx.AsyncClient, labels: list[str]) -> None:
    """Crea las etiquetas a NIVEL DE CUENTA si no existen (Ajustes → Etiquetas). Sin esto,
    Chatwoot aplica el tag a la conversación pero NO lo muestra en la UI. Cacheado."""
    acc = (f"{settings.chatwoot_url.rstrip('/')}/api/v1/accounts/{settings.chatwoot_account_id}/labels")
    missing = [l for l in labels if l not in _account_labels_cache]
    if not missing:
        return
    cur = await client.get(acc, headers=_headers())
    have = {l["title"] for l in cur.json().get("payload", [])} if cur.status_code == 200 else set()
    _account_labels_cache.update(have)
    for i, title in enumerate([l for l in missing if l not in have]):
        color = _LABEL_COLORS[(len(_account_labels_cache) + i) % len(_LABEL_COLORS)]
        try:
            await client.post(acc, headers=_headers(),
                              json={"title": title, "color": color, "show_on_sidebar": True})
            _account_labels_cache.add(title)
        except Exception:
            pass


async def add_conversation_labels(conversation_id: int, labels: list[str]) -> bool:
    """Agrega etiquetas a una conversación de Chatwoot SIN borrar las existentes (las une), y se
    asegura de que existan como etiqueta de CUENTA para que se vean en la UI. Sirve para
    diferenciar chats del bot (ej. por avatar/campaña) de otros usos del mismo número."""
    if not labels:
        return False
    base = (f"{settings.chatwoot_url.rstrip('/')}/api/v1/accounts/"
            f"{settings.chatwoot_account_id}/conversations/{conversation_id}/labels")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await _ensure_account_labels(client, labels)   # crea la etiqueta de cuenta si falta
            cur = await client.get(base, headers=_headers())
            existing = cur.json().get("payload", []) if cur.status_code == 200 else []
            merged = sorted(set(existing) | set(labels))
            if set(merged) == set(existing):
                return True  # ya estaban; nada que hacer
            r = await client.post(base, headers=_headers(), json={"labels": merged})
            return r.status_code < 400
    except Exception:
        return False


async def send_message(conversation_id: int, content: str, attachment_paths: list[str] | None = None) -> dict:
    if attachment_paths:
        attachment_paths = [_whatsapp_safe(p) for p in attachment_paths]
    url = (
        f"{settings.chatwoot_url.rstrip('/')}/api/v1/accounts/"
        f"{settings.chatwoot_account_id}/conversations/{conversation_id}/messages"
    )
    async with httpx.AsyncClient(timeout=60) as client:
        if attachment_paths:
            # multipart con archivos adjuntos (ej. CV del candidato)
            files = []
            handles = []
            for p in attachment_paths:
                try:
                    f = open(p, "rb")
                    handles.append(f)
                    files.append(("attachments[]", (os.path.basename(p), f)))
                except Exception:
                    continue
            try:
                r = await client.post(url, headers={"api_access_token": settings.chatwoot_token},
                                      data={"content": content, "message_type": "outgoing"}, files=files)
            finally:
                for f in handles:
                    f.close()
        else:
            r = await client.post(url, headers=_headers(),
                                  json={"content": content, "message_type": "outgoing"})
        r.raise_for_status()
        return r.json()


async def send_to_phone(phone: str, content: str, inbox_id: str | None = None,
                        attachment_paths: list[str] | None = None) -> dict | None:
    """Envía un WhatsApp saliente a un número arbitrario (ej. el personal del reclutador).

    Flujo Chatwoot: buscar/crear contacto → buscar/crear conversación en el inbox → mandar mensaje.
    """
    base = f"{settings.chatwoot_url.rstrip('/')}/api/v1/accounts/{settings.chatwoot_account_id}"
    inbox_id = inbox_id or settings.chatwoot_inbox_id
    async with httpx.AsyncClient(timeout=30) as client:
        # 1) buscar contacto por teléfono
        r = await client.get(f"{base}/contacts/search", headers=_headers(), params={"q": phone})
        r.raise_for_status()
        found = r.json().get("payload", [])
        contact = next((c for c in found if (c.get("phone_number") or "").replace(" ", "") == phone), None)

        # 2) crear contacto si no existe
        if contact is None:
            r = await client.post(f"{base}/contacts", headers=_headers(), json={
                "inbox_id": int(inbox_id), "name": f"Reclutador {phone}", "phone_number": phone,
            })
            r.raise_for_status()
            contact = r.json().get("payload", {}).get("contact") or r.json().get("payload", {})
        contact_id = contact.get("id")
        if not contact_id:
            return None

        # 3) buscar conversación existente del contacto en este inbox
        r = await client.get(f"{base}/contacts/{contact_id}/conversations", headers=_headers())
        convs = (r.json().get("payload") or []) if r.status_code == 200 else []
        conv = next((c for c in convs if str(c.get("inbox_id")) == str(inbox_id)), None)

        # 4) crear conversación si no hay
        if conv is None:
            # source_id del contact_inbox correspondiente
            source_id = None
            for ci in contact.get("contact_inboxes", []):
                if str(ci.get("inbox", {}).get("id")) == str(inbox_id):
                    source_id = ci.get("source_id")
            payload = {"inbox_id": int(inbox_id), "contact_id": contact_id}
            if source_id:
                payload["source_id"] = source_id
            r = await client.post(f"{base}/conversations", headers=_headers(), json=payload)
            r.raise_for_status()
            conv = r.json()
        conv_id = conv.get("id")
        if not conv_id:
            return None

    # 5) mandar el mensaje (con adjuntos si los hay) — reusa send_message
    return await send_message(conv_id, content, attachment_paths=attachment_paths)


async def _provider_config() -> dict:
    """Lee el provider_config del inbox de WhatsApp Cloud en Chatwoot (token + ids del WABA)."""
    base = f"{settings.chatwoot_url.rstrip('/')}/api/v1/accounts/{settings.chatwoot_account_id}"
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{base}/inboxes/{settings.chatwoot_inbox_id}", headers=_headers())
        r.raise_for_status()
        return r.json().get("provider_config", {}) or {}


async def send_whatsapp_template(phone: str, template_name: str, body_params: list[str],
                                 lang: str = "es") -> dict | None:
    """Envía una plantilla HSM aprobada vía Graph API (sirve FUERA de la ventana de 24h).
    `body_params` son los textos de las variables del cuerpo en orden ({{1}}, {{2}}, ...)."""
    cfg = await _provider_config()
    token = cfg.get("api_key")
    phone_number_id = cfg.get("phone_number_id")
    if not token or not phone_number_id or not phone:
        return None
    to = "".join(ch for ch in phone if ch.isdigit())
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": str(p)} for p in body_params],
            }],
        },
    }
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(f"https://graph.facebook.com/v21.0/{phone_number_id}/messages",
                              headers={"Authorization": f"Bearer {token}"}, json=payload)
        data = r.json()
        if r.status_code >= 400 or "error" in data:
            return {"error": data.get("error", data)}
        return data


async def template_status(template_name: str) -> str | None:
    """Devuelve el status (APPROVED/PENDING/REJECTED) de una plantilla del WABA, o None."""
    cfg = await _provider_config()
    token = cfg.get("api_key")
    waba = cfg.get("business_account_id")
    if not token or not waba:
        return None
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"https://graph.facebook.com/v21.0/{waba}/message_templates",
                             params={"access_token": token, "limit": 100, "fields": "name,status"})
        for t in r.json().get("data", []):
            if t.get("name") == template_name:
                return t.get("status")
    return None


async def download_attachment(data_url: str, dest_dir: str, file_name: str) -> str | None:
    """Download a candidate attachment to disk; return the local path."""
    if not data_url:
        return None
    os.makedirs(dest_dir, exist_ok=True)
    raw = file_name or data_url.split("/")[-1].split("?")[0]
    # Chatwoot a veces manda "archivo.pdf-filename*=UTF-8''..." → cortar la basura
    for junk in ["-filename*=", "-filename="]:
        i = raw.find(junk)
        if i > 0:
            raw = raw[:i]
            break
    safe = os.path.basename(raw) or "file"
    local_path = os.path.join(dest_dir, safe)
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(data_url)
            r.raise_for_status()
            with open(local_path, "wb") as f:
                f.write(r.content)
        return local_path
    except Exception:
        return None
