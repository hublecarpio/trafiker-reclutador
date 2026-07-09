"""Recruitment bot — Chatwoot webhook → role router → agent → reply.

Flujo humanizado (2026-06-01):
  1. Mensaje entra → se persiste + se acumula en Redis. NO se responde de inmediato.
  2. Si llegan más mensajes en la ventana (gente que escribe en ráfaga), se acumulan
     y el reloj se reinicia (debounce de DEBOUNCE_SECONDS).
  3. Pasados N segundos sin mensajes nuevos → el agente lee todo el contexto junto
     y genera UNA respuesta coherente.
  4. La respuesta se parte por párrafos (saltos de línea) y se envía mensaje por
     mensaje con delays de tipeo → se siente humano.

Endpoints:
  GET  /health            liveness
  GET  /stats             win ratio de portafolios (token)
  GET  /candidates        listado para RRHH (token)
  POST /webhook/chatwoot  Chatwoot message_created events
"""
import asyncio
import json
import re
import logging
import os
import zipfile
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text

from . import agenda, chatwoot, evolution, media
from .agent import classify_alert, evaluate_candidate, extract_facts, generate_reply
from .agent_config import get_agent_override
from .config import settings
from .tools import run_tools
from .db import SessionLocal, init_db
from .models import Applicant, Conversation, Document, Escalation, Interview, Message, PersonalMessage
from .roles import GENERIC, ROLES, detect_role

LIMA_TZ = ZoneInfo("America/Lima")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recruitbot")

app = FastAPI(title="TuEmpresa Recruitment Bot")

DOCS_DIR = "/data/documents"

# Roles que usan el flujo de revisión del reclutador (notificación de CV/audio + aprobación SI/NO).
# Agrega aquí los slugs de tus roles que deben avisar al reclutador cuando llega un candidato.
RECRUITER_REVIEW_ROLES = {"ejemplo-vendedor", "ejemplo-reclutador"}
# Roles con agenda de entrevistas por slots (booking automático de horarios).
SCHEDULING_ROLES = {"ejemplo-vendedor"}

redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def notify_recruiter(content: str, attachment_paths: list[str] | None = None) -> bool:
    """Canal de REPORTES al dueño (su WhatsApp personal). Va por EVOLUTION — no le pega la
    ventana de 24h del número oficial. Fallback a Chatwoot si Evolution no está disponible.
    OJO: esto es SOLO para avisos al reclutador; a los CANDIDATOS se les escribe por el
    número OFICIAL (chatwoot / plantillas), nunca por Evolution."""
    if evolution.enabled():
        try:
            r = await evolution.send_text(settings.recruiter_phone, content)
            if r and not (isinstance(r, dict) and r.get("error")):
                for p in (attachment_paths or [])[:3]:
                    await evolution.send_document(settings.recruiter_phone, p)
                return True
            log.warning("evolution notify devolvió error (%s) — fallback chatwoot", r)
        except Exception:
            log.exception("evolution notify falló — fallback chatwoot")
    try:
        await chatwoot.send_to_phone(settings.recruiter_phone, content, attachment_paths=attachment_paths)
        return True
    except Exception:
        log.exception("no pude notificar al reclutador por ningún canal")
        return False


async def _notify_flota_lead(db, applicant, conversation, chatwoot_conv_id, role_title, desc):
    """Captación de FLOTA: le manda al dueño la descripción del carro + un ZIP con las fotos que
    mandó el lead, para tenerlas a la mano. Dedup por cantidad de fotos: avisa al calificar y
    re-envía si después llegan más fotos. No rompe la respuesta si falla."""
    try:
        docs = (await db.execute(
            select(Document).where(Document.applicant_id == applicant.id).order_by(Document.id)
        )).scalars().all()
        photos = [d.local_path for d in docs if d.local_path and os.path.exists(d.local_path)]
        try:
            prev = await redis_client.get(f"flota_bundle:{conversation.id}")
            prev_n = int(prev) if prev is not None else -1
        except Exception:
            prev_n = -1
        # enviar si: primera calificación (hay desc y nunca se envió) o llegaron MÁS fotos que antes
        if not ((desc is not None and prev_n < 0) or (len(photos) > prev_n)):
            return
        # Solo lo ESENCIAL: Marca/Modelo/Año/Km/Transmisión + fotos. Sin URL ni ficha completa.
        parts = [p.strip() for p in (desc or "").split("|")]

        def _g(i):
            return parts[i] if i < len(parts) and parts[i] and parts[i] != "-" else ""
        # ficha [CALIFICA]: 0 nombre | 1 marca | 2 modelo | 3 transmisión | 4 año | 5 km
        marca, modelo, trans, anio, km = _g(1), _g(2), _g(3), _g(4), _g(5)
        veh = " ".join(x for x in [marca, modelo, anio] if x) or (applicant.name or "vehículo")
        km_txt = (km if "km" in km.lower() else f"{km} km") if km else ""
        detalle = " · ".join(x for x in [km_txt, trans] if x)
        lines = ["\U0001F697 *Carro para flota*" if desc else "\U0001F4F7 *Más fotos (flota)*", f"*{veh}*"]
        if detalle:
            lines.append(detalle)
        lines.append(f"\U0001F4F1 {applicant.name or 'Lead'} · {applicant.phone or ''}")
        lines.append(f"\U0001F4F7 {len(photos)} foto(s) adjuntas" if photos else "sin fotos todavía")
        body = "\n".join(lines)
        zip_path = None
        if photos:
            safe = "".join(ch for ch in (applicant.name or "lead") if ch.isalnum())[:20] or "lead"
            zip_path = f"/tmp/flota_{conversation.id}_{safe}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, p in enumerate(photos, 1):
                    ext = os.path.splitext(p)[1] or ".jpg"
                    zf.write(p, arcname=f"{safe}_{i}{ext}")
        await notify_recruiter(body, attachment_paths=[zip_path] if zip_path else None)
        await redis_client.set(f"flota_bundle:{conversation.id}", str(len(photos)), ex=60 * 60 * 24 * 30)
    except Exception:
        log.exception("no pude enviar el bundle de flota para conv %s", conversation.id)


async def _materialize_fleet_unit(db, applicant, conversation, califica_info, disponibilidad_info):
    """Marketplace de flota: crea/actualiza la fleet_unit del inventario a partir del lead
    capta-flota calificado. Parsea la ficha del [CALIFICA], engancha sus fotos (Document.unit_id)
    y registra las ventanas del [DISPONIBILIDAD]. status='pending' hasta que el dueño la apruebe."""
    from .models import Document, FleetUnit, FleetWindow
    from .date_logic import parse_availability
    try:
        unit = (await db.execute(select(FleetUnit).where(
            FleetUnit.source_conversation_id == conversation.id))).scalar_one_or_none()
        f = [x.strip() for x in (califica_info or "").split("|")]

        def g(i):
            return f[i] if i < len(f) and f[i] and f[i] != "-" else None

        def num(s):
            d = "".join(c for c in (s or "") if c.isdigit())
            return int(d) if d else None

        if unit is None:
            unit = FleetUnit(slug=f"unit-{conversation.id}", source_conversation_id=conversation.id,
                             owner_applicant_id=applicant.id, status="pending", ownership="consignment")
            db.add(unit)
        unit.owner_name = g(0) or applicant.name
        unit.make, unit.model, unit.transmission = g(1), g(2), g(3)
        unit.year, unit.km = num(g(4)), num(g(5))
        unit.price_ref_usd = num(g(6))
        unit.zone = g(7)
        unit.owner_phone = applicant.phone
        await db.flush()  # asigna unit.id
        # enganchar las fotos del dueño a la unidad
        docs = (await db.execute(select(Document).where(Document.applicant_id == applicant.id))).scalars().all()
        photos = [d for d in docs if d.local_path]
        for d in photos:
            d.unit_id = unit.id
        unit.photos_count = len(photos)
        # ventanas de disponibilidad (solo la primera vez, evita duplicar)
        if disponibilidad_info:
            existing = (await db.execute(select(FleetWindow).where(
                FleetWindow.fleet_unit_id == unit.id))).scalars().all()
            if not existing:
                av = parse_availability(disponibilidad_info)
                if av["weekdays"]:
                    db.add(FleetWindow(fleet_unit_id=unit.id, kind="available",
                                       weekdays=",".join(str(w) for w in av["weekdays"]),
                                       raw_text=disponibilidad_info))
                for (s, e) in av["windows"]:
                    db.add(FleetWindow(fleet_unit_id=unit.id, kind="available",
                                       start_date=s.date(), end_date=e.date(), raw_text=disponibilidad_info))
        await db.flush()
    except Exception:
        log.exception("no pude materializar fleet_unit para conv %s", conversation.id)


DOC_MARKER = "[DOC]"  # marcador en el buffer para señales de archivo recibido
KEY_TTL = 3600        # limpieza automática de claves redis

# Fotos reales: el agente las envía cuando el cliente las pide (una sola vez por conversación).
MAZDA_PHOTO_DIR = "/data/documents/mazda"
MAZDA_PHOTOS = ["mazda_ext_1.jpg", "mazda_ext_2.jpg", "mazda_int_1.jpg", "mazda_int_2.jpg", "mazda_int_3.jpg"]
# Fotos por rol de alquiler: {role_slug: (dir, [archivos], caption)}
RENTAL_PHOTOS = {
    "alquiler-mazda-cx5": (MAZDA_PHOTO_DIR, MAZDA_PHOTOS,
                           "Te dejo unas fotos del Mazda CX-5 (exterior e interior) 🚙👇"),
    "alquiler-territory": ("/data/documents/territory",
                           ["Ford foto 1.png", "Ford foto 2.png", "Ford foto 3 tablero.png",
                            "Ford foto 4 trasera.png", "Ford foto 5 interno.png", "Ford foto 6 maletera.png"],
                           "Te dejo fotos reales del Ford Territory 2024 (exterior, tablero e interior) 🚙👇"),
}
# (el mapa rol→vehículo de la agenda vive en app/agenda.py: agenda.vehicle_for_role)
PHOTO_INTENT_WORDS = ("foto", "fotos", "fotito", "imagen", "imagenes", "imágenes", "ver el carro",
                      "ver la camioneta", "ver el auto", "ver el vehiculo", "ver el vehículo", "ver la unidad")

# El cliente pide hablar con una PERSONA real → se alerta al WhatsApp personal del dueño.
HUMAN_INTENT_WORDS = ("hablar con una persona", "con una persona", "con un humano", "un humano",
                      "persona real", "humano real", "con alguien del equipo", "con alguien real",
                      "asesor humano", "agente humano", "quiero hablar con alguien", "pasame con",
                      "pásame con", "comunicarme con alguien", "comunicarme con una persona",
                      "atienda una persona", "hablar con un encargado", "me atienda alguien",
                      "alguien humano", "hablar con alguien", "con un representante", "atención humana",
                      # pedir LLAMADA / contacto telefónico → también es handoff humano
                      "me llamen", "me llames", "me llamas", "me puedes llamar", "me puede llamar",
                      "puedes llamarme", "puede llamarme", "llámame", "llamame", "llámenme", "llamenme",
                      "que me llamen", "te llamo", "le llamo", "puedo llamar", "le puedo llamar",
                      "los puedo llamar", "por llamada", "una llamada", "por teléfono", "por telefono",
                      "hablar por teléfono", "hablar por telefono", "número para llamar",
                      "numero para llamar", "llamada telefónica", "prefiero llamar", "quiero que me llamen")

# Etiqueta de Chatwoot por campaña/rol → diferencia los chats del bot (mismo número, varios usos).
# Nombre amigable para los de autos; para el resto se usa el propio slug del rol.
ROLE_LABELS = {
    "alquiler-territory": "territory",
    "alquiler-mazda-cx5": "mazda-cx5",
    "capta-flota": "capta-flota",
}


def label_for_role(slug: str) -> str:
    return ROLE_LABELS.get(slug, slug)

# Plan de invitación-fallback (un-tap): si los ya invitados no confirman antes del corte,
# el bot le avisa al reclutador y, solo si responde DALE, invita a candidatos de respaldo.
FALLBACK_FILE = "/data/documents/fallback_invite.json"  # bajo el volumen persistente


@app.on_event("startup")
async def _startup():
    await init_db()
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    log.info("DB ready. Roles: %s", ", ".join(ROLES))
    log.info("LLM agent: %s", "OpenRouter gpt-4.1-mini" if settings.openrouter_api_key else "templated fallback")
    log.info("Redis (debounce %ss): %s", settings.debounce_seconds, "OK" if redis_ok else "NO DISPONIBLE — respuestas inmediatas")
    asyncio.create_task(reminder_loop())
    log.info("Reminder loop activo (confirma citas la noche previa a las %s:00 Lima)", settings.reminder_hour)
    asyncio.create_task(fallback_loop())
    log.info("Fallback loop activo (invitación de respaldo un-tap)")
    asyncio.create_task(autos_lead_loop())
    log.info("Autos lead loop activo (polling form Meta cada 5 min): %s", settings.autos_form_id)
    asyncio.create_task(followup_loop())
    log.info("Seguimiento loop activo (recontacta cotizados fríos de alquiler cada hora)")
    asyncio.create_task(escalation_answer_loop())
    log.info("Escalación loop activo (envía al cliente la respuesta que el dueño escribe en el link)")


# ---------- Recordatorio automático la noche anterior ----------

async def send_due_reminders(force_date=None) -> int:
    """Envía recordatorio a los candidatos con entrevista MAÑANA (o force_date para test).
    Idempotente: marca reminder_sent. Devuelve cuántos recordatorios mandó."""
    now = datetime.now(LIMA_TZ)
    target = force_date or (now + timedelta(days=1)).date()
    sent = 0
    async with SessionLocal() as db:
        ivs = (await db.execute(
            select(Interview).where(Interview.status == "scheduled",
                                    Interview.reminder_sent.is_(False))
        )).scalars().all()
        for iv in ivs:
            when = iv.scheduled_at.astimezone(LIMA_TZ)
            if when.date() != target:
                continue
            applicant = (await db.execute(select(Applicant).where(Applicant.id == iv.applicant_id))).scalar_one()
            conv = (await db.execute(select(Conversation).where(Conversation.applicant_id == iv.applicant_id))).scalars().first()
            if not conv:
                continue
            msg = (
                f"¡Hola {applicant.name or ''}! 👋 Te recordamos tu entrevista:\n\n"
                f"🗓 *{DIAS[when.weekday()].capitalize()} {when.strftime('%d/%m')} a las {when.strftime('%H:%M')}*\n"
                f"📍 Sede {(iv.sede or 'Principal').capitalize()}\n\n"
                f"No olvides traer tu *documento de identidad vigente*. Al llegar, pregunta por el responsable de RRHH.\n\n"
                f"¿Confirmas tu asistencia? Responde *SÍ* para confirmar o avísame si necesitas reprogramar. 🙌"
            )
            try:
                await chatwoot.send_message(conv.chatwoot_conversation_id, msg)
                db.add(Message(conversation_id=conv.id, direction="outgoing", content=msg))
                iv.reminder_sent = True
                sent += 1
                log.info("recordatorio enviado a %s (cita %s)", applicant.name, when)
            except Exception:
                log.exception("no pude enviar recordatorio a applicant %s", iv.applicant_id)
        await db.commit()
    # resumen al reclutador
    if sent:
        try:
            await notify_recruiter(
                f"🔔 Envié {sent} recordatorio(s) de entrevista para mañana ({target.strftime('%d/%m')}). "
                f"Les pedí confirmar asistencia.")
        except Exception:
            pass
    return sent


async def reminder_loop():
    """Cada 10 min revisa si ya es la hora del recordatorio nocturno y, de serlo,
    confirma las citas del día siguiente (una sola vez por cita, gracias a reminder_sent)."""
    await asyncio.sleep(20)  # margen tras el arranque
    while True:
        try:
            now = datetime.now(LIMA_TZ)
            if now.hour >= settings.reminder_hour and now.hour < 23:
                await send_due_reminders()
        except Exception:
            log.exception("reminder_loop error")
        await asyncio.sleep(600)  # 10 min


# ---------- Invitación-fallback (un-tap: el bot pregunta, el reclutador dispara con DALE) ----------

def _load_fallback() -> dict | None:
    try:
        with open(FALLBACK_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def _save_fallback(plan: dict) -> None:
    try:
        with open(FALLBACK_FILE, "w") as f:
            json.dump(plan, f)
    except Exception:
        log.exception("no pude guardar el plan fallback")


def _fallback_offer_msg(name: str, slots: list[datetime]) -> str:
    opts = "\n".join(f"   • {DIAS[s.weekday()].capitalize()} {s.strftime('%d/%m')} a las {s.strftime('%H:%M')}"
                     for s in slots)
    return (
        f"¡Hola {name or ''}! 👋 Te escribo del equipo de reclutamiento.\n\n"
        f"Se abrió un espacio para entrevista presencial *mañana* y me encantaría coordinar contigo. 🎉\n\n"
        f"📍 Sede principal · ⏱ 20 minutos\n\n"
        f"¿Te quedaría bien alguno de estos horarios?\n{opts}\n\n"
        f"Por protocolo de seguridad, el día de la entrevista es indispensable traer tu *documento de identidad vigente* "
        f"(se validan tus datos de domicilio de forma presencial).\n\n"
        f"¿Cuál te acomoda? 🙌"
    )


async def _evaluate_fallback(plan: dict) -> None:
    """En el corte: ¿algún invitado tiene cita futura? Si sí, no molesta. Si no, pinga al reclutador."""
    target = datetime.strptime(plan["target_date"], "%Y-%m-%d").date()
    target_start = datetime.combine(target, time.min).replace(tzinfo=LIMA_TZ)
    async with SessionLocal() as db:
        confirmed = []
        for pid in plan["primary_ids"]:
            iv = (await db.execute(select(Interview).where(
                Interview.applicant_id == pid, Interview.status == "scheduled",
                Interview.scheduled_at >= target_start))).scalars().first()
            if iv:
                a = (await db.execute(select(Applicant).where(Applicant.id == pid))).scalar_one_or_none()
                confirmed.append((a.name if a else f"#{pid}"))
        cands = []
        for cid in plan["candidate_ids"]:
            a = (await db.execute(select(Applicant).where(Applicant.id == cid))).scalar_one_or_none()
            if a:
                cands.append((cid, a.name, a.eval_score))
    tgt_lbl = f"{DIAS[datetime.strptime(plan['target_date'], '%Y-%m-%d').weekday()]} {target.strftime('%d/%m')}"
    if confirmed:
        plan["status"] = "cancelled"
        _save_fallback(plan)
        await notify_recruiter(
            f"✅ Buenas: ya tienes cita confirmada para mañana con {', '.join(confirmed)}.\n"
            f"No disparo el fallback de los 3 nuevos. Si igual quieres invitarlos para {tgt_lbl} 12-1pm, responde *DALE*.")
    else:
        plan["status"] = "pinged"
        _save_fallback(plan)
        lines = "\n".join(f"   • {n} #{cid} ({s if s is not None else '?'})" for cid, n, s in cands)
        await notify_recruiter(
            f"🌙 Corte {plan['cutoff_hour']:02d}:{plan.get('cutoff_min', 0):02d} — *ninguno de los invitados confirmó* cita.\n\n"
            f"¿Invito a estos {len(cands)} para *{tgt_lbl} de 12 a 1pm*?\n{lines}\n\n"
            f"Responde *DALE* para enviarles la invitación, o ignóralo si no quieres. 🤖")


async def _fire_fallback() -> list[str]:
    """Envía la invitación de respaldo a los candidatos del plan (los aprueba + les ofrece slots).
    Marca el plan como 'fired'. Devuelve los nombres a los que se envió."""
    plan = _load_fallback()
    if not plan or plan.get("status") == "fired":
        return []
    tgt = datetime.strptime(plan["target_date"], "%Y-%m-%d")
    slots_dt = [tgt.replace(hour=int(t.split(":")[0]), minute=int(t.split(":")[1]),
                            second=0, microsecond=0, tzinfo=LIMA_TZ) for t in plan["slots"]]
    sent = []
    async with SessionLocal() as db:
        for cid in plan["candidate_ids"]:
            a = (await db.execute(select(Applicant).where(Applicant.id == cid))).scalar_one_or_none()
            if not a:
                continue
            a.approved_for_interview = True
            conv = (await db.execute(select(Conversation).where(Conversation.applicant_id == cid))).scalar_one_or_none()
            if conv:
                offer = _fallback_offer_msg(a.name, slots_dt)
                try:
                    await chatwoot.send_message(conv.chatwoot_conversation_id, offer)
                    db.add(Message(conversation_id=conv.id, direction="outgoing", content=offer))
                    sent.append(a.name or f"#{cid}")
                except Exception:
                    log.exception("no pude enviar invitación fallback a %s", cid)
        await db.commit()
    plan["status"] = "fired"
    _save_fallback(plan)
    return sent


async def fallback_loop():
    """Cada 5 min: en el corte avisa (un-tap); si hay un envío programado y llegó su hora, lo dispara."""
    await asyncio.sleep(30)
    while True:
        try:
            plan = _load_fallback()
            now = datetime.now(LIMA_TZ)
            if plan and plan.get("status") == "armed":
                cut = datetime.strptime(plan["date"], "%Y-%m-%d").replace(
                    hour=int(plan["cutoff_hour"]), minute=int(plan.get("cutoff_min", 0)),
                    second=0, microsecond=0, tzinfo=LIMA_TZ)
                if now >= cut:
                    await _evaluate_fallback(plan)
            elif plan and plan.get("status") == "scheduled_send" and plan.get("send_at"):
                if now >= datetime.fromisoformat(plan["send_at"]):
                    sent = await _fire_fallback()
                    tgt = datetime.strptime(plan["target_date"], "%Y-%m-%d")
                    tgt_lbl = f"{DIAS[tgt.weekday()]} {tgt.strftime('%d/%m')}"
                    await notify_recruiter(
                        f"🚀 (programado) Invité a: {', '.join(sent) or '(nadie)'}.\n"
                        f"Les pregunté por {tgt_lbl} de 12 a 1pm. Te aviso apenas alguno confirme su cita. 🤖")
        except Exception:
            log.exception("fallback_loop error")
        await asyncio.sleep(300)  # 5 min


@app.get("/health")
async def health():
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    return {"ok": True, "llm": bool(settings.openrouter_api_key), "redis": redis_ok,
            "debounce_seconds": settings.debounce_seconds, "roles": list(ROLES)}


def _auth(request: Request) -> bool:
    return not settings.webhook_secret or request.query_params.get("token") == settings.webhook_secret


@app.get("/stats")
async def stats(request: Request):
    """Win ratio de recolección de portafolios, global y por rol."""
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    from sqlalchemy import text
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT a.role_slug,"
            " count(DISTINCT a.id) AS postulantes,"
            " count(DISTINCT d.applicant_id) AS con_portafolio"
            " FROM applicants a LEFT JOIN documents d ON d.applicant_id=a.id"
            " GROUP BY a.role_slug ORDER BY postulantes DESC"
        ))).all()
        per_role = []
        tot_p = tot_d = 0
        for r in rows:
            p, d = r.postulantes, r.con_portafolio
            tot_p += p; tot_d += d
            per_role.append({"role": r.role_slug, "postulantes": p, "con_portafolio": d,
                             "win_ratio_pct": round(100 * d / p, 1) if p else 0})
        return {"ok": True,
                "global": {"postulantes": tot_p, "con_portafolio": tot_d,
                           "win_ratio_pct": round(100 * tot_d / tot_p, 1) if tot_p else 0},
                "por_rol": per_role}


@app.get("/candidates")
async def candidates(request: Request, stage: str | None = None, role: str | None = None):
    """Lista candidatos (filtrable por stage/role) con su pre-score para RRHH."""
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    q = select(Applicant).order_by(Applicant.eval_score.desc().nullslast(), Applicant.id.desc())
    if stage:
        q = q.where(Applicant.stage == stage)
    if role:
        q = q.where(Applicant.role_slug == role)
    async with SessionLocal() as db:
        apps = (await db.execute(q)).scalars().all()
        out = []
        for a in apps:
            ndocs = (await db.execute(
                select(func.count(Document.id)).where(Document.applicant_id == a.id)
            )).scalar()
            out.append({"id": a.id, "name": a.name, "phone": a.phone, "role": a.role_slug,
                        "stage": a.stage, "score": a.eval_score, "notes": a.eval_notes, "docs": ndocs})
        return {"ok": True, "count": len(out), "candidates": out}


@app.post("/cron/reminders")
async def cron_reminders(request: Request, date: str | None = None):
    """Disparo manual del recordatorio (para pruebas o cron externo).
    ?date=YYYY-MM-DD fuerza la fecha objetivo de las entrevistas a recordar."""
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    force = None
    if date:
        try:
            force = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            return {"ok": False, "error": "fecha inválida, usa YYYY-MM-DD"}
    sent = await send_due_reminders(force_date=force)
    return {"ok": True, "reminders_sent": sent, "target": str(force) if force else "tomorrow"}


def _is_incoming(payload: dict) -> bool:
    mt = payload.get("message_type")
    return mt in ("incoming", 0, "0")


# ---------- Humanizador: parser de respuesta + delays de tipeo ----------

def split_reply(text: str) -> list[str]:
    """Parte la respuesta en mensajes separados por párrafos (saltos de línea dobles).
    Si un solo párrafo es enorme, lo deja entero (no cortamos frases a la mitad)."""
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not parts:
        parts = [text.strip()] if text.strip() else []
    return parts


def typing_delay(chunk: str) -> float:
    """Delay proporcional al largo del mensaje — simula tipeo humano."""
    est = len(chunk) * 0.035  # ~28 chars/seg
    return max(settings.typing_delay_min, min(settings.typing_delay_max, est))


# ---------- Agendamiento: slots de entrevista ----------

async def available_slots(db, role_slug: str, days_ahead: int = 5) -> list[datetime]:
    """Slots libres de lunes a viernes para los próximos N días hábiles.
    La agenda es ÚNICA POR CAMPAÑA (role_slug): todas las entrevistas del mismo
    puesto comparten al gerente y la sala, así que un slot ocupado por cualquier
    candidato de la campaña bloquea a todos (sin importar su sede).
    Horarios desde settings.interview_slots (ej. '10:00,10:30'), 20 min c/u."""
    times = [t.strip() for t in settings.interview_slots.split(",") if t.strip()]
    taken = set(
        dt.astimezone(LIMA_TZ).strftime("%Y-%m-%d %H:%M")
        for (dt,) in (await db.execute(
            select(Interview.scheduled_at).where(
                Interview.role_slug == role_slug, Interview.status == "scheduled")
        )).all()
    )
    slots = []
    day = datetime.now(LIMA_TZ)
    checked = 0
    while len(slots) < days_ahead * len(times) and checked < 14:
        day = day + timedelta(days=1)
        checked += 1
        if day.weekday() >= 5:  # sáb/dom no hay entrevistas
            continue
        for t in times:
            hh, mm = t.split(":")
            slot = day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if slot.strftime("%Y-%m-%d %H:%M") not in taken:
                slots.append(slot)
    return slots


def slots_context(slots: list[datetime]) -> str:
    if not slots:
        return "[SLOTS DISPONIBLES]: ninguno esta semana — dile al candidato que lo contactaremos para coordinar."
    lines = [f"   • {DIAS[s.weekday()]} {s.strftime('%d/%m')} a las {s.strftime('%H:%M')}" for s in slots[:6]]
    return ("[SLOTS DISPONIBLES] para entrevista presencial en la sede principal "
            "(ofrece máximo 2-3 opciones, las más próximas):\n" + "\n".join(lines))


async def notify_recruiter_interview(applicant: Applicant, sede: str, when: datetime):
    """Avisa al WhatsApp personal del reclutador que se agendó una cita."""
    msg = (
        f"📅 *CITA AGENDADA — {ROLES.get(applicant.role_slug, GENERIC).title}*\n\n"
        f"👤 {applicant.name or 'Sin nombre'}\n"
        f"📱 {applicant.phone or '?'}\n"
        f"🏢 Sede: {sede.capitalize()}\n"
        f"🗓 {DIAS[when.weekday()].capitalize()} {when.strftime('%d/%m/%Y')} a las {when.strftime('%H:%M')}\n"
        + (f"⭐ Pre-score: {applicant.eval_score}/100\n" if applicant.eval_score else "")
        + f"\nAgendado automáticamente por el bot. 🤖"
    )
    try:
        await notify_recruiter(msg)
        return True
    except Exception as e:
        log.exception("no pude notificar al reclutador: %s", e)
        return False


# ---------- Comandos del reclutador (desde su WhatsApp personal) ----------

RECRUITER_HELP = (
    "🤖 *Comandos disponibles:*\n\n"
    "• *SI <id>* — aprobar candidato y ofrecerle cita\n"
    "• *NO <id>* — rechazar candidato (no se le ofrece cita)\n"
    "• *PENDIENTES* — ver candidatos esperando tu aprobación\n"
    "• *AGENDA* — ver citas agendadas\n"
    "• *VER <id>* — leer la conversación completa de un candidato\n"
    "• *DALE* — disparar la invitación de respaldo pendiente\n"
    "• *AYUDA* — ver estos comandos"
)


def _interview_offer_msg(name: str, slots: list[datetime]) -> str:
    opts = "\n".join(f"   • {DIAS[s.weekday()].capitalize()} {s.strftime('%d/%m')} a las {s.strftime('%H:%M')}"
                     for s in slots[:3])
    return (
        f"¡Hola {name or ''}! 👋 Buenas noticias sobre tu postulación.\n\n"
        f"Tu perfil pasó la evaluación y queremos conocerte en una entrevista presencial. 🎉\n\n"
        f"📍 Sede principal · ⏱ 20 minutos\n\n"
        f"Horarios disponibles:\n{opts}\n\n"
        f"Por protocolo de seguridad, el día de la entrevista es indispensable:\n"
        f"• Traer tu *documento de identidad vigente*\n"
        f"• Se validarán tus datos de domicilio de forma presencial\n\n"
        f"¿Cuál horario te queda mejor? 🙌"
    )


# ── Aprobación de candidatos: lógica COMPARTIDA entre el comando de WhatsApp ('SI/NO <id>')
#    y los endpoints /internal usados por el DASHBOARD. Un solo lugar = mismo comportamiento. ──
async def _approve_applicant(db, aid: int) -> dict:
    """Aprueba: marca approved_for_interview=True y, si el rol usa agenda por slots,
    OFRECE los horarios al candidato; si no, le avisa que pasó (Meet). Igual que 'SI <id>'."""
    applicant = (await db.execute(select(Applicant).where(Applicant.id == aid))).scalar_one_or_none()
    if not applicant:
        return {"ok": False, "error": "not_found", "id": aid}
    applicant.approved_for_interview = True
    conv = (await db.execute(select(Conversation).where(Conversation.applicant_id == aid))).scalar_one_or_none()
    mode = "meet"
    if applicant.role_slug in SCHEDULING_ROLES:
        mode = "slots"
        slots = await available_slots(db, applicant.role_slug)
        if conv and slots:
            offer = _interview_offer_msg(applicant.name, slots)
            try:
                await chatwoot.send_message(conv.chatwoot_conversation_id, offer)
                db.add(Message(conversation_id=conv.id, direction="outgoing", content=offer))
            except Exception:
                log.exception("no pude enviar la oferta al candidato")
    else:
        if conv:
            msg = (f"¡Felicitaciones {applicant.name or ''}! 🎉 Pasaste a la siguiente etapa del proceso en TuEmpresa.\n\n"
                   f"La entrevista es por *Google Meet*. Nuestro equipo se contactará contigo muy pronto para "
                   f"coordinar el día y la hora, y enviarte el enlace. ¡Prepárate! 💪")
            try:
                await chatwoot.send_message(conv.chatwoot_conversation_id, msg)
                db.add(Message(conversation_id=conv.id, direction="outgoing", content=msg))
            except Exception:
                log.exception("no pude avisar al candidato")
    await db.commit()
    return {"ok": True, "id": aid, "name": applicant.name, "phone": applicant.phone, "mode": mode}


async def _reject_applicant(db, aid: int) -> dict:
    """Rechaza (approved_for_interview=False). Igual que 'NO <id>'."""
    applicant = (await db.execute(select(Applicant).where(Applicant.id == aid))).scalar_one_or_none()
    if not applicant:
        return {"ok": False, "error": "not_found", "id": aid}
    applicant.approved_for_interview = False
    await db.commit()
    return {"ok": True, "id": aid, "name": applicant.name}


@app.post("/internal/approve/{aid}")
async def internal_approve(aid: int, request: Request):
    """Aprobación desde el DASHBOARD (mismo efecto que 'SI <id>' por WhatsApp)."""
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    async with SessionLocal() as db:
        return await _approve_applicant(db, aid)


@app.post("/internal/reject/{aid}")
async def internal_reject(aid: int, request: Request):
    """Rechazo desde el DASHBOARD (mismo efecto que 'NO <id>')."""
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
    async with SessionLocal() as db:
        return await _reject_applicant(db, aid)


async def handle_recruiter_command(content: str, recruiter_conv_id: int) -> dict:
    """Procesa órdenes que el reclutador manda desde su WhatsApp personal."""
    text = (content or "").strip().upper()

    async def reply(msg: str):
        try:
            await chatwoot.send_message(recruiter_conv_id, msg)
        except Exception:
            log.exception("no pude responder al reclutador")

    async with SessionLocal() as db:
        # --- SI <id> / APROBAR <id> ---
        m = None
        import re as _re
        if (m := _re.match(r"^(SI|APROBAR|APRUEBA)\s+(\d+)$", text)):
            aid = int(m.group(2))
            res = await _approve_applicant(db, aid)
            if not res["ok"]:
                await reply(f"❌ No encuentro al candidato con id {aid}. Manda PENDIENTES para ver la lista.")
                return {"ok": True, "recruiter_command": "approve", "found": False}
            if res["mode"] == "slots":
                await reply(f"✅ *{res['name']}* (id {aid}) aprobado.\nLe ofrecí los horarios disponibles. Te aviso cuando confirme. 🤖")
            else:
                await reply(f"✅ *{res['name']}* (id {aid}) aprobado.\n"
                            f"Le avisé que pasó y que coordinarás la *entrevista por Meet* con él.\n"
                            f"📱 {res['phone']} — agéndale el Meet cuando quieras. 🤖")
            return {"ok": True, "recruiter_command": "approve", "applicant": aid}

        # --- NO <id> / RECHAZAR <id> ---
        if (m := _re.match(r"^(NO|RECHAZAR|RECHAZA)\s+(\d+)$", text)):
            aid = int(m.group(2))
            res = await _reject_applicant(db, aid)
            if not res["ok"]:
                await reply(f"❌ No encuentro al candidato con id {aid}.")
                return {"ok": True, "recruiter_command": "reject", "found": False}
            await reply(f"❌ *{res['name']}* (id {aid}) marcado como rechazado. No se le ofrecerá cita.")
            return {"ok": True, "recruiter_command": "reject", "applicant": aid}

        # --- PENDIENTES ---
        if text in ("PENDIENTES", "PENDIENTE", "LISTA"):
            rows = (await db.execute(
                select(Applicant).where(
                    Applicant.role_slug.in_(RECRUITER_REVIEW_ROLES),
                    Applicant.approved_for_interview.is_(None),
                    Applicant.eval_score.isnot(None),
                ).order_by(Applicant.eval_score.desc()).limit(15)
            )).scalars().all()
            if not rows:
                await reply("📭 No hay candidatos pendientes de aprobación.")
            else:
                lines = [f"• *{a.name}* — score {a.eval_score} — id *{a.id}*\n  sede: {a.sede or '?'} | {a.phone}"
                         for a in rows]
                await reply("📋 *Candidatos esperando tu aprobación:*\n\n" + "\n\n".join(lines) +
                            "\n\nResponde *SI <id>* para agendar o *NO <id>* para descartar.")
            return {"ok": True, "recruiter_command": "pendientes", "count": len(rows)}

        # --- AGENDA ---
        if text in ("AGENDA", "CITAS"):
            rows = (await db.execute(
                select(Interview, Applicant).join(Applicant, Applicant.id == Interview.applicant_id)
                .where(Interview.status == "scheduled").order_by(Interview.scheduled_at)
            )).all()
            if not rows:
                await reply("📭 No hay citas agendadas.")
            else:
                lines = [f"• {DIAS[iv.scheduled_at.astimezone(LIMA_TZ).weekday()].capitalize()} "
                         f"{iv.scheduled_at.astimezone(LIMA_TZ).strftime('%d/%m %H:%M')} — *{a.name}* ({a.phone}) — {iv.sede}"
                         for iv, a in rows]
                await reply("📅 *Citas agendadas:*\n\n" + "\n".join(lines))
            return {"ok": True, "recruiter_command": "agenda", "count": len(rows)}

        # --- VER <id>: reenviar la conversación completa de Chatwoot ---
        if (m := _re.match(r"^VER\s+(\d+)$", text)):
            aid = int(m.group(1))
            applicant = (await db.execute(select(Applicant).where(Applicant.id == aid))).scalar_one_or_none()
            if not applicant:
                await reply(f"❌ No encuentro al candidato con id {aid}.")
                return {"ok": True, "recruiter_command": "ver", "found": False}
            conv = (await db.execute(select(Conversation).where(Conversation.applicant_id == aid))).scalar_one_or_none()
            msgs = []
            if conv:
                msgs = (await db.execute(
                    select(Message).where(Message.conversation_id == conv.id).order_by(Message.id)
                )).scalars().all()
            if not msgs:
                await reply(f"📭 No hay conversación registrada de *{applicant.name or aid}*.")
                return {"ok": True, "recruiter_command": "ver", "count": 0}
            header = (f"💬 *Conversación — {applicant.name or 'Sin nombre'}* (id {aid})\n"
                      f"📱 {applicant.phone or '?'}"
                      + (f" · ⭐ {applicant.eval_score}/100" if applicant.eval_score else "") + "\n"
                      "──────────")
            # WhatsApp corta mensajes largos: partir en bloques de ~12 líneas
            lines = [f"{'👤' if mm.direction == 'incoming' else '🤖'} {mm.content}"
                     for mm in msgs if (mm.content or '').strip()]
            await reply(header)
            block = []
            for ln in lines:
                block.append(ln)
                if len(block) >= 12:
                    await reply("\n".join(block))
                    block = []
            if block:
                await reply("\n".join(block))
            return {"ok": True, "recruiter_command": "ver", "count": len(lines)}

        # --- DALE / INVITAR: disparar YA la invitación-fallback pendiente ---
        if text in ("DALE", "INVITAR", "DISPARA", "DISPARAR", "ENVIAR"):
            plan = _load_fallback()
            if not plan or plan.get("status") == "fired":
                await reply("ℹ️ No hay una invitación de respaldo pendiente por disparar.")
                return {"ok": True, "recruiter_command": "dale", "fired": False}
            sent = await _fire_fallback()
            tgt = datetime.strptime(plan["target_date"], "%Y-%m-%d")
            tgt_lbl = f"{DIAS[tgt.weekday()]} {tgt.strftime('%d/%m')}"
            await reply(f"🚀 Invitación enviada a: {', '.join(sent) or '(nadie — sin conversación)'}.\n"
                        f"Les pregunté por {tgt_lbl} de 12 a 1pm. Te aviso apenas alguno confirme su cita. 🤖")
            return {"ok": True, "recruiter_command": "dale", "sent": len(sent)}

        # --- CANCELAR FALLBACK: descartar el plan de respaldo ---
        if text in ("CANCELAR FALLBACK", "CANCELAR", "NO DISPARES"):
            plan = _load_fallback()
            if plan and plan.get("status") != "fired":
                plan["status"] = "cancelled"
                _save_fallback(plan)
                await reply("🛑 Listo, cancelé la invitación de respaldo. No se enviará nada.")
            else:
                await reply("ℹ️ No hay invitación de respaldo activa para cancelar.")
            return {"ok": True, "recruiter_command": "cancelar_fallback"}

    # --- cualquier otra cosa → ayuda ---
    await reply(RECRUITER_HELP)
    return {"ok": True, "recruiter_command": "help"}


async def notify_recruiter_qualification(applicant: Applicant, score: int | None, note: str | None, cv_paths: list[str]):
    """Cuando un candidato manda su CV: avisar al reclutador con el CV adjunto (score si lo hay)."""
    score_line = f"⭐ Pre-score: {score}/100\n" if score is not None else "⭐ Pre-score: (pendiente)\n"
    sede_line = f"🏢 Sede: {applicant.sede}\n" if applicant.sede else ""
    msg = (
        f"🔔 *NUEVO — {ROLES.get(applicant.role_slug, GENERIC).title}*\n\n"
        f"👤 *{applicant.name or 'Sin nombre'}*\n"
        f"📱 {applicant.phone or '?'}\n"
        + sede_line
        + score_line
        + (f"📝 {note}\n" if note else "")
        + f"📇 Ficha: {settings.dash_url.rstrip('/')}/candidate/{applicant.id}\n"
        + f"\n¿Avanzas con este candidato?\n"
        f"Responde: *SI {applicant.id}* · *NO {applicant.id}*"
    )
    try:
        await notify_recruiter(msg, attachment_paths=cv_paths[:2] or None)
        return True
    except Exception:
        log.exception("no pude notificar calificación al reclutador")
        return False


# ---------- Debounce: acumulador de inputs en Redis ----------

async def _schedule_reply(db_conv_id: int, chatwoot_conv_id: int, role_slug: str, my_seq: int):
    """Espera DEBOUNCE_SECONDS; si no llegó nada más nuevo, procesa el buffer y responde."""
    await asyncio.sleep(settings.debounce_seconds)
    try:
        cur = await redis_client.get(f"seq:{db_conv_id}")
    except Exception:
        cur = None
    if cur is not None and int(cur) != my_seq:
        return  # llegó un mensaje más nuevo → esa tarea responderá

    # tomar y limpiar el buffer
    try:
        buffered = await redis_client.lrange(f"buffer:{db_conv_id}", 0, -1)
        await redis_client.delete(f"buffer:{db_conv_id}")
    except Exception:
        buffered = []
    has_new_docs = any(b.startswith(DOC_MARKER) for b in buffered)
    # ¿el cliente pidió hablar con una persona real?
    _joined_in = " ".join(buffered).lower()
    wants_human = any(w in _joined_in for w in HUMAN_INTENT_WORDS)

    role = ROLES.get(role_slug) or GENERIC

    async with SessionLocal() as db:
        conversation = (await db.execute(
            select(Conversation).where(Conversation.id == db_conv_id))).scalar_one_or_none()
        if conversation is None:
            return

        # --- HANDOFF HUMANO: si la conversación está pausada, el bot NO responde (el dueño atiende) ---
        # El mensaje entrante ya quedó guardado en la DB; solo nos quedamos callados para no interferir.
        if getattr(conversation, "bot_paused", False):
            log.info("bot en pausa (handoff humano) para conv %s — no respondo", conversation.id)
            return

        applicant = (await db.execute(
            select(Applicant).where(Applicant.id == conversation.applicant_id))).scalar_one()

        # ¿primera respuesta de esta conversación?
        outgoing_count = (await db.execute(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation.id, Message.direction == "outgoing")
        )).scalar()

        # contexto dinámico: slots de entrevista
        # ⚠️ SOLO si el RECLUTADOR aprobó a este candidato (approved_for_interview=True).
        # Sin aprobación, el agente NUNCA ofrece citas — solo recolecta info y dice "te contactaremos".
        extra_ctx = ""
        scheduling_enabled = (role.slug in SCHEDULING_ROLES
                              and applicant.approved_for_interview is True)
        if scheduling_enabled:
            slots = await available_slots(db, role.slug)  # agenda por campaña
            extra_ctx = slots_context(slots)
        elif role.slug in SCHEDULING_ROLES:
            extra_ctx = ("[IMPORTANTE]: este candidato AÚN NO está aprobado para entrevista. "
                         "NO le ofrezcas citas ni horarios. Si pregunta por la entrevista, dile que su "
                         "postulación está en evaluación y que lo contactaremos pronto por este medio.")

        if wants_human:
            extra_ctx += ("\n[El cliente pidió hablar con una PERSONA real. Dile con calidez que ya "
                          "avisaste a alguien del equipo y que lo contactarán muy pronto por aquí; no lo "
                          "dejes en visto y mantén el tono humano. NO digas que eres un bot.]")

        if outgoing_count == 0 and (role.intro or "").strip():
            # roles con intro fija (saludo guionado) → primer turno scripted
            reply = role.intro
            applicant.stage = "informed_timeline"
        else:
            # roles SIN intro (ej. vendedor-vehiculo por CTWA): el primer turno también
            # va al LLM, así nunca se manda un saludo vacío que deja al lead colgado.
            history = (await db.execute(
                select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
            )).scalars().all()
            hist = [
                {"role": "user" if m.direction == "incoming" else "assistant", "content": m.content or ""}
                for m in history if (m.content or "").strip()
            ]
            # TOOLS del agente (config en agents.tools, editable en el dashboard): se corren
            # antes del LLM e inyectan datos duros al contexto (ej. cálculo de fechas).
            _ov = await get_agent_override(role.slug)
            _enabled_tools = (_ov or {}).get("tools") or []
            _convtext = " ".join(h["content"] for h in hist if h["role"] == "user")
            if _enabled_tools:
                # tools de LECTURA (fechas, disponibilidad de la agenda) → inyectan contexto duro
                _toolctx = await run_tools(_enabled_tools,
                                           {"conv_text": _convtext, "role_slug": role.slug}, db)
                if _toolctx:
                    extra_ctx = (extra_ctx + "\n" + _toolctx) if extra_ctx else _toolctx
            reply = await generate_reply(role, hist, has_new_docs, extra_context=extra_ctx)
            if has_new_docs:
                applicant.stage = "docs_received"

        # --- ESCALACIÓN: el agente marcó que NO supo responder → lo ocultamos al cliente y avisamos ---
        escalate_reason = None
        if reply and "[ESCALAR:" in reply:
            mm = re.search(r"\[ESCALAR:(.*?)\]", reply, re.DOTALL)
            escalate_reason = (mm.group(1).strip() if mm else "(no especificado)")
            reply = re.sub(r"\[ESCALAR:.*?\]", "", reply, flags=re.DOTALL).strip()
            if not reply:
                reply = "Dame un momento, ahora te confirmo esa información 🙌"

        # --- CALIFICA: el agente marcó un lead CALIFICADO (ej. mentoría) → avisar al dueño ---
        califica_info = None
        if reply and "[CALIFICA:" in reply:
            cm = re.search(r"\[CALIFICA:(.*?)\]", reply, re.DOTALL)
            califica_info = (cm.group(1).strip() if cm else "(lead calificado)")
            reply = re.sub(r"\[CALIFICA:.*?\]", "", reply, flags=re.DOTALL).strip()
            if not reply:
                reply = "¡Tu perfil encaja! 🙌 El equipo te escribe personalmente para los detalles."

        # --- DISPONIBILIDAD: el dueño de flota dijo cuándo puede alquilar su carro (marcador interno) ---
        disponibilidad_info = None
        if reply and "[DISPONIBILIDAD" in reply:
            dm = re.search(r"\[DISPONIBILIDAD:(.*?)\]", reply, re.DOTALL)
            disponibilidad_info = (dm.group(1).strip() if dm else "")
            reply = re.sub(r"\[DISPONIBILIDAD:?.*?\]", "", reply, flags=re.DOTALL).strip()

        # --- RESERVAR: el cliente hizo el DEPÓSITO / mandó voucher → bloquear las fechas en la agenda ---
        reservar_info = None
        if reply and "[RESERVAR" in reply:
            rm = re.search(r"\[RESERVAR:(.*?)\]", reply, re.DOTALL)
            reservar_info = (rm.group(1).strip() if rm else "(reserva)")
            reply = re.sub(r"\[RESERVAR:?.*?\]", "", reply, flags=re.DOTALL).strip()
            if not reply:
                reply = "¡Listo! Dejé tus fechas reservadas 🙌 El equipo te confirma en breve."

        # --- HUMANO: el agente detectó algo que requiere a una persona (ej. otra unidad/flota) ---
        # → manda su mensaje (ej. "lo revisamos con la flota") y luego PAUSA el bot para que el dueño
        #   atienda sin que el bot interfiera. Marcador interno, el cliente NO lo ve.
        handoff_info = None
        if reply and "[HUMANO" in reply:
            hm = re.search(r"\[HUMANO:(.*?)\]", reply, re.DOTALL)
            handoff_info = (hm.group(1).strip() if hm else "(requiere atención humana)")
            reply = re.sub(r"\[HUMANO:?.*?\]", "", reply, flags=re.DOTALL).strip()
            if not reply:
                reply = "Déjame revisarlo con el equipo y un asesor te confirma las opciones por aquí 🙌"

        # --- FOTOS: el agente pidió enviar las fotos reales del vehículo (marcador interno) ---
        wants_photos = False
        if reply and "[FOTOS]" in reply:
            wants_photos = True
            reply = reply.replace("[FOTOS]", "").strip()
            if not reply:
                reply = "¡Claro! Te paso unas fotos del auto 🚙"

        # --- envío humanizado: partir por párrafos y mandar uno por uno ---
        chunks = split_reply(reply)
        for i, chunk in enumerate(chunks):
            try:
                await chatwoot.send_message(chatwoot_conv_id, chunk)
            except Exception as e:
                log.exception("send failed: %s", e)
            db.add(Message(conversation_id=conversation.id, direction="outgoing", content=chunk))
            if i < len(chunks) - 1:
                await asyncio.sleep(typing_delay(chunks[i + 1]))

        # --- FOTOS reales del vehículo cuando el cliente las pide (una sola vez por conversación) ---
        if role.slug in RENTAL_PHOTOS:
            asked = " ".join(buffered).lower()
            if wants_photos or any(w in asked for w in PHOTO_INTENT_WORDS):
                try:
                    already = await redis_client.get(f"rental_photos_sent:{conversation.id}")
                except Exception:
                    already = None
                if not already:
                    pdir, pfiles, caption = RENTAL_PHOTOS[role.slug]
                    photo_paths = [f"{pdir}/{n}" for n in pfiles if os.path.exists(f"{pdir}/{n}")]
                    if photo_paths:
                        try:
                            await chatwoot.send_message(chatwoot_conv_id, caption, attachment_paths=photo_paths)
                            db.add(Message(conversation_id=conversation.id, direction="outgoing",
                                           content=caption + f" [+{len(photo_paths)} fotos]"))
                            await redis_client.set(f"rental_photos_sent:{conversation.id}", "1", ex=60 * 60 * 24 * 30)
                        except Exception as e:
                            log.exception("envío de fotos del vehículo falló: %s", e)

        # --- HANDOFF HUMANO: el cliente pidió una persona, O el agente emitió [HUMANO] (ej. quiere
        #     otra unidad/flota). En ambos casos PAUSAMOS el bot (deja de responder) para que el dueño
        #     atienda sin interferencia, y le avisamos a su WhatsApp con el link al chat. ---
        if wants_human or handoff_info:
            from datetime import datetime as _dt, timezone as _tz
            ult = next((b for b in reversed(buffered) if not b.startswith(DOC_MARKER)), "")
            reason = handoff_info or (f"pidió hablar con una persona: \"{ult[:140]}\"")
            # pausa el bot para esta conversación (idempotente)
            if not getattr(conversation, "bot_paused", False):
                conversation.bot_paused = True
                conversation.pause_reason = reason[:300]
                conversation.paused_at = _dt.now(_tz.utc)
            try:
                already = await redis_client.get(f"human_alert:{conversation.id}")
            except Exception:
                already = None
            if not already:
                chat_link = (f"{settings.chatwoot_url}/app/accounts/{settings.chatwoot_account_id}"
                             f"/conversations/{chatwoot_conv_id}")
                alert = (f"🙋 *ATENCIÓN HUMANA — bot PAUSADO* — {role.title}\n"
                         f"{applicant.name or 'Lead'} · {applicant.phone or ''}\n"
                         f"Motivo: {reason[:180]}\n"
                         f"El bot dejó de responder esta conversación. Atiéndela tú 👇\n"
                         f"👉 {chat_link}\n"
                         f"(Para que el bot vuelva, reactívalo desde el panel de Autos.)")
                try:
                    await notify_recruiter(alert)
                    await redis_client.set(f"human_alert:{conversation.id}", "1", ex=60 * 60 * 24)
                except Exception as e:
                    log.exception("no pude alertar handoff humano: %s", e)

        # --- ESCALACIÓN CON RESPUESTA: el bot no supo algo (caso no cubierto por política) →
        #     pausa el bot (queda CALLADO) + te manda un LINK para que escribas la respuesta;
        #     un loop la recoge, la pule con IA y se la envía al cliente. ---
        if escalate_reason:
            try:
                ek = await redis_client.get(f"escalate_alert:{conversation.id}")
            except Exception:
                ek = None
            if not ek:
                import secrets as _secrets
                from datetime import datetime as _dt, timezone as _tz
                token = _secrets.token_urlsafe(24)
                db.add(Escalation(token=token, conversation_id=conversation.id,
                                  chatwoot_conversation_id=chatwoot_conv_id, role_slug=role.slug,
                                  client_name=applicant.name, client_phone=applicant.phone,
                                  question=(escalate_reason or "")[:500], status="pending"))
                # pausa el bot para que quede CALLADO hasta que el dueño responda
                conversation.bot_paused = True
                conversation.pause_reason = f"esperando tu respuesta: {(escalate_reason or '')[:120]}"
                conversation.paused_at = _dt.now(_tz.utc)
                link = f"{settings.answer_base_url.rstrip('/')}/answer/{token}"
                alert = (f"🧠 *EL BOT NO SUPO RESPONDER* — {role.title}\n"
                         f"{applicant.name or 'Lead'} · {applicant.phone or ''}\n"
                         f"No supo: \"{escalate_reason[:180]}\"\n"
                         f"✍️ Escribe la respuesta aquí y el bot se la manda al cliente:\n{link}")
                try:
                    await notify_recruiter(alert)
                    await redis_client.set(f"escalate_alert:{conversation.id}", "1", ex=600)
                except Exception as e:
                    log.exception("no pude alertar escalación: %s", e)

        # --- CAPTACIÓN DE FLOTA: lead calificado o llegaron fotos → mandarle al dueño la
        #     descripción del carro + ZIP de fotos (con dedup por cantidad de fotos) ---
        if role.slug == "capta-flota" and (califica_info or has_new_docs):
            if califica_info:
                await _materialize_fleet_unit(db, applicant, conversation, califica_info, disponibilidad_info)
            await _notify_flota_lead(db, applicant, conversation, chatwoot_conv_id,
                                     role.title, califica_info)

        # --- LEAD CALIFICADO (mentoría/otros) → avisar al dueño para que cierre personalmente ---
        if califica_info and role.slug != "capta-flota":
            # Persistir: marca al applicant como 'calificado' y guarda la ficha, para que el lead
            # entre al EMBUDO del dashboard (etapa 'Calificado' = pasó el filtro) y a casos exitosos.
            applicant.stage = "calificado"
            applicant.eval_notes = califica_info[:2000]
            try:
                ck = await redis_client.get(f"califica_alert:{conversation.id}")
            except Exception:
                ck = None
            if not ck:
                chat_link = (f"{settings.chatwoot_url}/app/accounts/{settings.chatwoot_account_id}"
                             f"/conversations/{chatwoot_conv_id}")
                ficha_link = f"{settings.dash_url.rstrip('/')}/candidate/{applicant.id}"
                alert = (f"\U0001F7E2 *LEAD CALIFICADO — {role.title}*\n"
                         f"{applicant.name or 'Lead'} · {applicant.phone or ''}\n"
                         f"{califica_info[:220]}\n"
                         f"\U0001F4C7 Ficha completa: {ficha_link}\n"
                         f"\U0001F449 Chat: {chat_link}")
                try:
                    await notify_recruiter(alert)
                    await redis_client.set(f"califica_alert:{conversation.id}", "1", ex=60 * 60 * 24 * 7)
                except Exception as e:
                    log.exception("no pude alertar lead calificado: %s", e)

        # --- RESERVA POR DEPÓSITO: el cliente mandó voucher → BLOQUEA las fechas + avisa para verificar ---
        if reservar_info:
            try:
                rk = await redis_client.get(f"reservar_done:{conversation.id}")
            except Exception:
                rk = None
            veh = agenda.vehicle_for_role(role.slug)
            if not rk and veh and "reservar" in ((await get_agent_override(role.slug) or {}).get("tools") or []):
                try:
                    utext = " ".join(m.content or "" for m in (await db.execute(
                        select(Message).where(Message.conversation_id == conversation.id,
                                              Message.direction == "incoming").order_by(Message.id)
                    )).scalars().all())
                    # Las fechas del MARCADOR ([RESERVAR: ... | fechas | ...]) son las que el agente
                    # ya confirmó con el cliente → priorízalas; el historial es respaldo.
                    booking_text = f"{reservar_info} {utext}"
                    booking = await agenda.reserve(db, veh, booking_text, applicant.phone, reservar_info[:200])
                    chat_link = (f"{settings.chatwoot_url}/app/accounts/{settings.chatwoot_account_id}"
                                 f"/conversations/{chatwoot_conv_id}")
                    if booking:
                        rango = f"{booking.start_date:%d/%m}→{booking.end_date:%d/%m}"
                        alert = (f"\U0001F4B0 *DEPÓSITO / RESERVA — {role.title}*\n"
                                 f"{applicant.name or 'Lead'} · {applicant.phone or ''}\n"
                                 f"{reservar_info[:200]}\n"
                                 f"📅 Reserva #{booking.id} {rango} → fechas BLOQUEADAS (estado: por verificar).\n"
                                 f"✅ VERIFICA el voucher en tu BCP. Si es real: territory_agenda.py confirmar {booking.id}\n"
                                 f"❌ Si es falso/no llega: territory_agenda.py cancelar {booking.id}\n"
                                 f"\U0001F449 {chat_link}")
                        await redis_client.set(f"reservar_done:{conversation.id}", "1", ex=60 * 60 * 24 * 30)
                    else:
                        # no se pudo bloquear (choque, sin fecha, o antes del piso) → avisar igual para revisar a mano
                        alert = (f"⚠️ *DEPÓSITO sin poder reservar auto — {role.title}*\n"
                                 f"{applicant.name or 'Lead'} · {applicant.phone or ''}\n"
                                 f"{reservar_info[:200]}\n"
                                 f"No pude bloquear las fechas (choque, sin fecha clara o antes del 25/06). Revísalo: {chat_link}")
                    await notify_recruiter(alert)
                except Exception as e:
                    log.exception("reserva por depósito falló: %s", e)

        # transcript completo (incluye la respuesta que acabamos de dar)
        transcript = "\n".join(f"{m.direction}: {m.content}" for m in (await db.execute(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
        )).scalars().all()) + "\n" + "\n".join(f"outgoing: {c}" for c in chunks)

        # pre-evaluación cuando llegan documentos — SOLO la primera vez (evita score flip-flop).
        if has_new_docs and settings.openrouter_api_key and applicant.eval_score is None:
            score, note = await evaluate_candidate(role, transcript)
            if score is not None:
                applicant.eval_score = score
                applicant.eval_notes = note
                applicant.stage = "in_review"
        # Notificar al reclutador UNA sola vez cuando el candidato MANDA SU CV (tenga score o no).
        # El score es un extra; lo que NO puede fallar es que te llegue el CV.
        if (role.slug in RECRUITER_REVIEW_ROLES and applicant.approved_for_interview is None
                and not applicant.recruiter_notified):
            cv_paths = [d.local_path for d in (await db.execute(
                select(Document).where(Document.applicant_id == applicant.id,
                                       Document.local_path.isnot(None))
            )).scalars().all()]
            if cv_paths:  # solo si realmente hay un documento que reenviarte
                ok = await notify_recruiter_qualification(
                    applicant, applicant.eval_score, applicant.eval_notes, cv_paths)
                applicant.recruiter_notified = bool(ok)

        # Reenviar al reclutador cualquier AUDIO o VIDEO nuevo del candidato (parte del proceso).
        if role.slug in RECRUITER_REVIEW_ROLES and has_new_docs:
            AV_EXT = (".ogg", ".oga", ".mp3", ".m4a", ".aac", ".wav", ".opus",  # audio
                      ".mp4", ".mov", ".webm", ".3gp", ".m4v", ".mkv")           # video
            av_paths = [
                d.local_path for d in (await db.execute(
                    select(Document).where(Document.applicant_id == applicant.id,
                                           Document.local_path.isnot(None))
                )).scalars().all()
                if d.local_path and (any(d.local_path.lower().endswith(e) for e in AV_EXT)
                                     or (d.kind or "").lower() in ("audio", "video"))
            ]
            # solo reenviar los de ESTE batch (los recién llegados)
            new_av = [p for p in av_paths if any(os.path.basename(p) in dn or dn in p for dn in doc_names)]
            if new_av:
                try:
                    await notify_recruiter(
                        f"🎙️ *Audio/Video* — {applicant.name or '?'} ({applicant.phone})\n"
                        f"Presentación / capacidad resolutiva (parte del proceso):",
                        attachment_paths=new_av[:2])
                except Exception:
                    log.exception("no pude reenviar audio/video al reclutador")

        # --- ALERTA EN TIEMPO REAL: si el candidato manda una señal de riesgo (queja, desistimiento,
        #     molestia, problema de agenda), avisar al reclutador a su personal con motivo + extracto.
        #     Dedupe 6h por candidato vía Redis para no spamear. ---
        incoming_text = "\n".join(b for b in buffered if not b.startswith(DOC_MARKER)).strip()
        if role.slug in RECRUITER_REVIEW_ROLES and incoming_text:
            try:
                already_alerted = await redis_client.get(f"alerted:{applicant.id}")
            except Exception:
                already_alerted = None
            if not already_alerted:
                verdict = await classify_alert(role.title, incoming_text)
                if verdict.get("alert"):
                    sev = verdict.get("severidad", "media")
                    icon = {"alta": "🚨", "media": "⚠️", "baja": "ℹ️"}.get(sev, "⚠️")
                    recent = (await db.execute(
                        select(Message).where(Message.conversation_id == conversation.id)
                        .order_by(Message.id.desc()).limit(6))).scalars().all()
                    snippet = "\n".join(
                        f"{'👤' if mm.direction == 'incoming' else '🤖'} {mm.content}"
                        for mm in reversed(recent) if (mm.content or '').strip())
                    alert_msg = (
                        f"{icon} *ALERTA — {role.title}*\n\n"
                        f"👤 *{applicant.name or 'Sin nombre'}* (id {applicant.id})\n"
                        f"📱 {applicant.phone or '?'}\n"
                        + (f"⭐ Pre-score: {applicant.eval_score}/100\n" if applicant.eval_score else "")
                        + f"🔴 {verdict.get('tipo', '?')} · severidad {sev}\n"
                        f"📝 {verdict.get('motivo', '')}\n\n"
                        f"*Últimos mensajes:*\n{snippet}\n\n"
                        f"Para ver toda la conversación responde: *VER {applicant.id}*"
                    )
                    try:
                        await notify_recruiter(alert_msg)
                        await redis_client.set(f"alerted:{applicant.id}", "1", ex=6 * 3600)
                        log.info("alerta enviada al reclutador por applicant %s (%s)", applicant.id, verdict.get("tipo"))
                    except Exception:
                        log.exception("no pude enviar alerta al reclutador")

        # --- extracción de hechos: sede elegida + cita confirmada ---
        # GUARDIA DURA UNIVERSAL: jamás se crea/ofrece una cita sin la aprobación explícita
        # del reclutador (SI <id> desde su WhatsApp personal). Vale para TODOS los roles.
        if scheduling_enabled and applicant.approved_for_interview is not True:
            scheduling_enabled = False
        if scheduling_enabled and settings.openrouter_api_key and outgoing_count > 0:
            facts = await extract_facts(transcript)
            # sede
            if facts.get("sede") and not applicant.sede:
                applicant.sede = facts["sede"]
                log.info("applicant %s sede registrada: %s", applicant.id, facts["sede"])
            # cita confirmada → guardar + notificar (con guardas anti-cruce y anti-duplicado)
            if facts.get("interview_confirmed") and facts.get("fecha") and facts.get("hora"):
                # 1) ¿ya tiene una cita activa este candidato? → no crear otra
                existing_iv = (await db.execute(
                    select(Interview).where(Interview.applicant_id == applicant.id,
                                            Interview.status == "scheduled"))).scalar_one_or_none()
                if existing_iv is not None:
                    log.info("applicant %s ya tiene cita activa — no duplico", applicant.id)
                else:
                    try:
                        when = datetime.strptime(f"{facts['fecha']} {facts['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=LIMA_TZ)
                        sede = applicant.sede or facts.get("sede") or "independencia"
                        # 2) ¿el slot ya está tomado en ESTA campaña? → no double-booking
                        clash = (await db.execute(
                            select(Interview).where(
                                Interview.role_slug == role.slug,
                                Interview.status == "scheduled",
                                Interview.scheduled_at == when))).scalar_one_or_none()
                        if clash is not None:
                            log.warning("slot %s ya ocupado en %s — el agente reofrecerá otros", when, role.slug)
                            warn = ("Disculpa, ese horario se acaba de ocupar. "
                                    "Te paso los horarios que siguen disponibles en el próximo mensaje. 🙏")
                            try:
                                await chatwoot.send_message(chatwoot_conv_id, warn)
                                db.add(Message(conversation_id=conversation.id, direction="outgoing", content=warn))
                            except Exception:
                                pass
                        else:
                            iv = Interview(applicant_id=applicant.id, role_slug=role.slug,
                                           sede=sede, scheduled_at=when, status="scheduled")
                            db.add(iv)
                            try:
                                await db.flush()  # dispara el índice único uq_interview_slot si hay carrera
                            except Exception:
                                await db.rollback()
                                raise RuntimeError("slot tomado en carrera")
                            applicant.stage = "interview_scheduled"
                            notified = await notify_recruiter_interview(applicant, sede, when)
                            iv.notified = notified
                            log.info("CITA agendada: applicant %s → %s %s (notif=%s)",
                                     applicant.id, facts["fecha"], facts["hora"], notified)
                    except Exception as e:
                        log.exception("error guardando cita: %s", e)

        await db.commit()
    log.info("replied conv %s (%s) — %s chunk(s), docs=%s", db_conv_id, role.slug, len(chunks), has_new_docs)


# ---------- AUTOS: motor de leads del formulario Meta (compra de camionetas, crédito BCP) ----------

async def _match_autos_lead(db, phone: str | None):
    """¿El que escribe es un LEAD DE AUTOS al que le mandamos la plantilla (vino por form)?
    Match por teléfono (últimos 9 dígitos). Solo si aún no tiene conversación."""
    if not phone:
        return None
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 8:
        return None
    cands = (await db.execute(select(Applicant).where(
        Applicant.role_slug == settings.autos_role, Applicant.phone.isnot(None)))).scalars().all()
    for a in cands:
        ad = "".join(c for c in (a.phone or "") if c.isdigit())
        if ad and (ad == digits or ad[-9:] == digits[-9:]):
            has_conv = (await db.execute(
                select(Conversation).where(Conversation.applicant_id == a.id))).scalar_one_or_none()
            if has_conv is None:
                return a
    return None


async def _autos_page_token() -> str | None:
    if not settings.meta_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            r = await cl.get(f"https://graph.facebook.com/v21.0/{settings.autos_page_id}",
                             params={"fields": "access_token", "access_token": settings.meta_token})
            return r.json().get("access_token")
    except Exception:
        log.exception("no pude obtener page token autos")
        return None


async def poll_autos_leads() -> int:
    """Baja leads nuevos del formulario Meta, los guarda, avisa al personal e inicia el WhatsApp
    con la plantilla del crédito BCP. Dedup por Redis (autos:processed)."""
    ptoken = await _autos_page_token()
    if not ptoken:
        return 0
    try:
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.get(f"https://graph.facebook.com/v21.0/{settings.autos_form_id}/leads",
                             params={"access_token": ptoken, "fields": "id,created_time,field_data", "limit": 50})
        leads = r.json().get("data", [])
    except Exception:
        log.exception("autos: no pude leer leads del form")
        return 0
    new = 0
    tpl_status = await chatwoot.template_status(settings.autos_template)
    for lead in leads:
        lid = str(lead.get("id"))
        try:
            if await redis_client.sismember("autos:processed", lid):
                continue
        except Exception:
            pass
        f = {}
        for fd in lead.get("field_data", []):
            vals = fd.get("values") or []
            f[fd.get("name")] = vals[0] if vals else ""
        name = f.get("full_name") or f.get("nombre") or ""
        phone = f.get("phone_number") or ""
        veh = " ".join(x for x in (f.get("marca", ""), f.get("modelo", ""), f.get("anio", "")) if x).strip()
        resumen_form = (f"FORM autos | {veh} | {f.get('km','?')} km | pide {f.get('precio','?')} | "
                        f"{f.get('distrito','?')} | en venta: {f.get('tiempo_venta','?')}")
        async with SessionLocal() as db:
            applicant = Applicant(chatwoot_contact_id=None, name=name, phone=phone,
                                  role_slug=settings.autos_role, stage="new", eval_notes=resumen_form)
            db.add(applicant)
            await db.commit()
        # iniciar WhatsApp con la plantilla del crédito BCP (si está aprobada)
        sent = False
        if tpl_status == "APPROVED" and phone:
            first = (name or "").split()[0] if name else "👋"
            res = await chatwoot.send_whatsapp_template(phone, settings.autos_template,
                                                        [first, veh or "tu camioneta"])
            sent = bool(res and "error" not in res)
        estado = ("✅ Ya le escribí por WhatsApp (plantilla BCP)." if sent
                  else (f"⏳ Plantilla BCP {tpl_status} — le escribo apenas Meta apruebe."
                        if tpl_status != "APPROVED" else "⚠️ No pude enviarle el WhatsApp aún."))
        await notify_recruiter(
            f"🚗 *NUEVO LEAD — vendedor de camioneta*\n👤 {name or '(sin nombre)'}\n📱 {phone or '?'}\n"
            f"🚙 {veh or '?'}\n📍 {f.get('distrito','?')}\n🔢 {f.get('km','?')} km\n💵 Pide: {f.get('precio','?')}\n"
            f"⏱ En venta: {f.get('tiempo_venta','?')}\n\n{estado}")
        try:
            await redis_client.sadd("autos:processed", lid)
        except Exception:
            pass
        new += 1
    return new


async def autos_lead_loop():
    """Cada 5 min revisa leads nuevos del formulario de autos."""
    await asyncio.sleep(45)
    while True:
        try:
            n = await poll_autos_leads()
            if n:
                log.info("autos: %s leads nuevos procesados", n)
        except Exception:
            log.exception("autos_lead_loop error")
        await asyncio.sleep(300)


@app.post("/cron/autos-leads")
async def cron_autos_leads(request: Request):
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    return {"ok": True, "new": await poll_autos_leads()}


# ---------- SEGUIMIENTO automático de leads de alquiler que cotizaron y se enfriaron ----------
FOLLOWUP_ROLES = ("alquiler-territory", "alquiler-mazda-cx5")
FOLLOWUP_MAX_PER_RUN = 25


async def followup_cold_quotes() -> int:
    """Leads de alquiler que COTIZARON pero se enfriaron (último msg del bot, sin pausa, sin
    reserva): les manda UN seguimiento. Dentro de 24h → texto libre; fuera → plantilla aprobada.
    Una sola vez por conversación (dedup en redis)."""
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            with lastmsg as (
              select distinct on (conversation_id) conversation_id, direction, created_at
              from messages order by conversation_id, id desc
            ), lastin as (
              select conversation_id, max(created_at) as last_in
              from messages where direction='incoming' group by conversation_id
            )
            select c.id, c.chatwoot_conversation_id as cw, a.name, a.phone, li.last_in
            from conversations c
            join applicants a on a.id=c.applicant_id
            join lastmsg lm on lm.conversation_id=c.id
            left join lastin li on li.conversation_id=c.id
            where c.role_slug = any(:roles)
              and c.bot_paused = false
              and lm.direction = 'outgoing'
              and lm.created_at < now() - interval '2 hours'
              and lm.created_at > now() - interval '8 days'
              and exists(select 1 from messages mm where mm.conversation_id=c.id
                         and mm.direction='outgoing' and mm.content ~ 'USD[[:space:]]*[0-9]')
            order by lm.created_at desc
        """), {"roles": list(FOLLOWUP_ROLES)})).all()

    sent = 0
    now = datetime.now(LIMA_TZ)
    for r in rows:
        if sent >= FOLLOWUP_MAX_PER_RUN:
            break
        try:
            if await redis_client.get(f"followup_sent:{r.id}"):
                continue
        except Exception:
            pass
        name = (r.name or "").strip().split(" ")[0] if (r.name or "").strip() else ""
        within24 = bool(r.last_in) and (now - r.last_in).total_seconds() < 24 * 3600
        try:
            if within24:
                msg = (f"Hola{(' ' + name) if name else ''} 👋 ¿Avanzamos con tu reserva del Ford Territory? "
                       f"Con la separación de S/100 te aseguro tus fechas 🚙 Cualquier duda, aquí estoy.")
                await chatwoot.send_message(r.cw, msg)
                logged = "[SEGUIMIENTO] " + msg
            else:
                rr = await chatwoot.send_whatsapp_template(
                    r.phone, "territory_alquiler_seguimiento_v1", [name or "👋"], "es")
                if rr and isinstance(rr, dict) and rr.get("error"):
                    raise RuntimeError(str(rr["error"])[:120])
                logged = "[SEGUIMIENTO plantilla territory_alquiler_seguimiento_v1]"
            async with SessionLocal() as db2:
                db2.add(Message(conversation_id=r.id, direction="outgoing", content=logged))
                await db2.commit()
            await redis_client.set(f"followup_sent:{r.id}", "1", ex=60 * 60 * 24 * 30)
            sent += 1
        except Exception:
            log.exception("seguimiento falló conv %s", r.id)
    return sent


async def followup_loop():
    """Cada hora revisa cotizados fríos y les manda seguimiento."""
    await asyncio.sleep(150)
    while True:
        try:
            n = await followup_cold_quotes()
            if n:
                log.info("seguimiento: %s leads fríos contactados", n)
        except Exception:
            log.exception("followup_loop error")
        await asyncio.sleep(3600)


@app.post("/cron/followups")
async def cron_followups(request: Request):
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    return {"ok": True, "contactados": await followup_cold_quotes()}


# ---------- HANDOFF CON RESPUESTA: recoge lo que el dueño escribió y se lo manda al cliente ----------
async def _polish_answer(question: str | None, answer: str | None, client_name: str | None) -> str | None:
    """Pule la respuesta del dueño como un mensaje de WhatsApp listo para el cliente (IA)."""
    from .agent import _chat
    prompt = (
        "Eres el asistente de TuEmpresa Autos por WhatsApp. El cliente preguntó algo que el bot no supo, y el "
        "dueño te dio la respuesta correcta. Redáctala como un mensaje de WhatsApp cálido, claro y breve "
        "(1-3 líneas), español de Lima (tú), listo para enviar. NO agregues datos que no estén en la "
        "respuesta del dueño. NO uses JSON ni comillas ni marcadores.\n\n"
        f"Cliente: {client_name or ''}\nPregunta del cliente: {question or ''}\n"
        f"Respuesta del dueño: {answer or ''}\n\nMensaje para el cliente:")
    try:
        out = await _chat([{"role": "user", "content": prompt}], max_tokens=220)
        return (out or "").strip() or None
    except Exception:
        return None


async def _send_escalation_answers() -> int:
    """Toma las escalaciones que el dueño ya respondió (status='answered'), pule con IA y envía al
    cliente por el número oficial; luego DESPAUSA la conversación para que el bot pueda seguir."""
    from datetime import datetime as _dt, timezone as _tz
    async with SessionLocal() as db:
        rows = (await db.execute(select(Escalation).where(Escalation.status == "answered"))).scalars().all()
    sent = 0
    for e in rows:
        try:
            final = (await _polish_answer(e.question, e.answer, e.client_name)) or e.answer
            if e.chatwoot_conversation_id and final:
                await chatwoot.send_message(e.chatwoot_conversation_id, final)
            async with SessionLocal() as db2:
                if e.conversation_id and final:
                    db2.add(Message(conversation_id=e.conversation_id, direction="outgoing", content=final))
                    conv = (await db2.execute(select(Conversation).where(
                        Conversation.id == e.conversation_id))).scalar_one_or_none()
                    if conv:
                        conv.bot_paused = False
                        conv.pause_reason = None
                esc = (await db2.execute(select(Escalation).where(Escalation.id == e.id))).scalar_one()
                esc.status = "sent"
                esc.sent_at = _dt.now(_tz.utc)
                await db2.commit()
            sent += 1
        except Exception:
            log.exception("no pude enviar la respuesta de escalación %s", e.id)
    return sent


async def escalation_answer_loop():
    """Cada 15s revisa si el dueño respondió alguna escalación y la manda al cliente."""
    await asyncio.sleep(60)
    while True:
        try:
            n = await _send_escalation_answers()
            if n:
                log.info("escalación: %s respuesta(s) enviadas al cliente", n)
        except Exception:
            log.exception("escalation_answer_loop error")
        await asyncio.sleep(15)


@app.post("/webhook/personal")
async def chatwoot_personal_webhook(request: Request):
    """Webhook de la cuenta Chatwoot del WhatsApp PERSONAL del dueño (Evolution integrado).
    Solo GUARDA los mensajes (no responde) para poder VER respuestas de candidatos al personal
    (ej. confirmaciones que no entran por el número oficial). Config en la nueva cuenta Chatwoot:
    URL = https://<tunnel>/webhook/personal?token=<webhook_secret> , evento 'Message created'."""
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    payload = await request.json()
    if payload.get("event") != "message_created":
        return {"ok": True, "skipped": payload.get("event")}
    sender = payload.get("sender") or {}
    mtype = payload.get("message_type")
    direction = "incoming" if mtype in (0, "incoming") else "outgoing"
    content = (payload.get("content") or "").strip()
    if not content:
        return {"ok": True, "skipped": "empty"}
    phone = sender.get("phone_number")
    name = sender.get("name")
    mid = payload.get("id")
    async with SessionLocal() as db:
        if mid:
            dup = (await db.execute(
                select(PersonalMessage).where(PersonalMessage.chatwoot_message_id == mid))).scalar_one_or_none()
            if dup:
                return {"ok": True, "dup": True}
        db.add(PersonalMessage(chatwoot_message_id=mid, contact_phone=phone, contact_name=name,
                               direction=direction, content=content))
        await db.commit()
    return {"ok": True, "stored": True, "direction": direction}


@app.get("/personal")
async def personal_messages(request: Request, limit: int = 50):
    """Ver los últimos mensajes del WhatsApp personal (auth por token)."""
    if not _auth(request):
        return {"ok": False, "error": "bad token"}
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(PersonalMessage).order_by(PersonalMessage.id.desc()).limit(min(limit, 200)))).scalars().all()
    return {"ok": True, "count": len(rows), "messages": [
        {"dir": r.direction, "phone": r.contact_phone, "name": r.contact_name,
         "content": r.content, "at": r.created_at.isoformat() if r.created_at else None}
        for r in rows]}


@app.post("/webhook/chatwoot")
async def chatwoot_webhook(request: Request):
    if not _auth(request):
        return {"ok": False, "error": "bad token"}

    payload = await request.json()
    event = payload.get("event")
    if event != "message_created" or not _is_incoming(payload):
        return {"ok": True, "skipped": event}

    content = (payload.get("content") or "").strip()
    conv = payload.get("conversation") or {}
    conv_id = conv.get("id") or payload.get("conversation_id")
    inbox = payload.get("inbox") or {}
    inbox_id = inbox.get("id") or conv.get("inbox_id")
    sender = payload.get("sender") or {}
    phone = sender.get("phone_number")
    contact_id = sender.get("id")
    name = sender.get("name")
    attachments = payload.get("attachments") or []

    if not conv_id:
        return {"ok": True, "skipped": "no conversation id"}

    # ¿Es el RECLUTADOR dando órdenes desde su WhatsApp personal? → flujo de comandos
    if phone and phone.replace(" ", "") == settings.recruiter_phone.replace(" ", ""):
        return await handle_recruiter_command(content, conv_id)

    async with SessionLocal() as db:
        existing = (
            await db.execute(select(Conversation).where(Conversation.chatwoot_conversation_id == conv_id))
        ).scalar_one_or_none()

        first_touch = existing is None
        if first_touch:
            # GATE ESTRICTO: solo si el mensaje contiene una frase clave de campaña.
            role = detect_role(content)
            applicant = None
            if role is None:
                # ¿es un LEAD DE AUTOS al que le escribimos por la plantilla (vino del form)?
                applicant = await _match_autos_lead(db, phone)
                if applicant is not None:
                    role = ROLES.get(settings.autos_role)
            if role is None:
                log.info("no role-phrase match on conv %s — bot stays silent", conv_id)
                return {"ok": True, "skipped": "no role phrase match"}
            if applicant is None:
                applicant = Applicant(
                    chatwoot_contact_id=contact_id, name=name, phone=phone, role_slug=role.slug, stage="new"
                )
                db.add(applicant)
                await db.flush()
            elif contact_id and not applicant.chatwoot_contact_id:
                applicant.chatwoot_contact_id = contact_id
            conversation = Conversation(
                chatwoot_conversation_id=conv_id,
                chatwoot_inbox_id=inbox_id,
                applicant_id=applicant.id,
                role_slug=role.slug,
            )
            db.add(conversation)
            await db.flush()
            # AUTO-ETIQUETA en Chatwoot según la campaña detectada por la frase clave del bot
            # (diferencia estos chats de los otros usos del mismo número). No bloquea si falla.
            try:
                await chatwoot.add_conversation_labels(conv_id, [label_for_role(role.slug)])
            except Exception:
                log.exception("no pude etiquetar la conversación %s", conv_id)
        else:
            conversation = existing
            applicant = (
                await db.execute(select(Applicant).where(Applicant.id == conversation.applicant_id))
            ).scalar_one()
            role = ROLES.get(conversation.role_slug) or GENERIC

        # archivos (CV / audio / imagen): descargar + ENTENDER (audio→Whisper, imagen→Gemini)
        # y dejar una ETIQUETA en texto, porque el agente SOLO lee texto. Una nota de voz entra
        # como mensaje sin texto; sin esto el agente "no la ve" y sigue pidiéndola.
        doc_names = []
        media_labels = []
        for idx, att in enumerate(attachments):
            data_url = att.get("data_url") or att.get("file_url")
            fname = att.get("file_name") or (data_url.split("/")[-1] if data_url else "file")
            att_id = att.get("id") or f"{payload.get('id', 'm')}_{idx}"
            fname = f"{att_id}_{fname}"
            local = await chatwoot.download_attachment(data_url, f"{DOCS_DIR}/{applicant.id}", fname)
            db.add(Document(
                applicant_id=applicant.id,
                kind=att.get("file_type") or "other",
                file_name=fname,
                source_url=data_url,
                local_path=local,
            ))
            doc_names.append(fname)
            if local:
                try:
                    media_labels.append(
                        await media.to_label(local, att.get("file_type"), att.get("file_name") or fname))
                except Exception:
                    log.exception("media.to_label falló")

        # mensaje entrante = texto + etiquetas de media (transcripción de audio, descripción de imagen…)
        incoming_content = " ".join(
            x for x in ([content] + [f"[{lbl}]" for lbl in media_labels]) if x).strip()
        db.add(Message(conversation_id=conversation.id, direction="incoming", content=incoming_content))

        db_conv_id = conversation.id
        role_slug = conversation.role_slug
        await db.commit()

    # --- acumular en redis + agendar respuesta con debounce ---
    try:
        if incoming_content:
            await redis_client.rpush(f"buffer:{db_conv_id}", incoming_content)
        for dn in doc_names:
            await redis_client.rpush(f"buffer:{db_conv_id}", f"{DOC_MARKER}{dn}")
        await redis_client.expire(f"buffer:{db_conv_id}", KEY_TTL)
        my_seq = await redis_client.incr(f"seq:{db_conv_id}")
        await redis_client.expire(f"seq:{db_conv_id}", KEY_TTL)
        asyncio.create_task(_schedule_reply(db_conv_id, conv_id, role_slug, my_seq))
        mode = f"debounced {settings.debounce_seconds}s"
    except Exception as e:
        # Redis caído → responder inmediato (fallback) para no dejar al lead colgado
        log.warning("redis no disponible (%s) — respuesta inmediata", e)
        asyncio.create_task(_schedule_reply_immediate(db_conv_id, conv_id, role_slug, bool(doc_names)))
        mode = "immediate (no redis)"

    return {"ok": True, "role": role_slug, "first_touch": first_touch,
            "docs": len(doc_names), "mode": mode}


async def _schedule_reply_immediate(db_conv_id: int, chatwoot_conv_id: int, role_slug: str, has_docs: bool):
    """Fallback sin redis: responde de inmediato (comportamiento anterior)."""
    try:
        await redis_client.set(f"seq:{db_conv_id}", 0)
    except Exception:
        pass
    # reusar la misma lógica con seq=0 (no habrá comparación posible, responde directo)
    await _reply_now(db_conv_id, chatwoot_conv_id, role_slug, has_docs)


async def _reply_now(db_conv_id: int, chatwoot_conv_id: int, role_slug: str, has_new_docs: bool):
    role = ROLES.get(role_slug) or GENERIC
    async with SessionLocal() as db:
        conversation = (await db.execute(
            select(Conversation).where(Conversation.id == db_conv_id))).scalar_one_or_none()
        if conversation is None:
            return

        # --- HANDOFF HUMANO: si la conversación está pausada, el bot NO responde (el dueño atiende) ---
        # El mensaje entrante ya quedó guardado en la DB; solo nos quedamos callados para no interferir.
        if getattr(conversation, "bot_paused", False):
            log.info("bot en pausa (handoff humano) para conv %s — no respondo", conversation.id)
            return

        applicant = (await db.execute(
            select(Applicant).where(Applicant.id == conversation.applicant_id))).scalar_one()
        outgoing_count = (await db.execute(
            select(func.count(Message.id)).where(
                Message.conversation_id == conversation.id, Message.direction == "outgoing")
        )).scalar()
        if outgoing_count == 0 and (role.intro or "").strip():
            reply = role.intro
            applicant.stage = "informed_timeline"
        else:
            history = (await db.execute(
                select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
            )).scalars().all()
            hist = [{"role": "user" if m.direction == "incoming" else "assistant", "content": m.content or ""}
                    for m in history if (m.content or "").strip()]
            reply = await generate_reply(role, hist, has_new_docs)
            if has_new_docs:
                applicant.stage = "docs_received"
        for i, chunk in enumerate(split_reply(reply)):
            try:
                await chatwoot.send_message(chatwoot_conv_id, chunk)
            except Exception:
                log.exception("send failed")
            db.add(Message(conversation_id=conversation.id, direction="outgoing", content=chunk))
            await asyncio.sleep(typing_delay(chunk))
        await db.commit()
