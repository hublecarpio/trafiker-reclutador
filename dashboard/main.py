"""
Dashboard de SOLO LECTURA para el sistema de reclutamiento + campañas de TuEmpresa.
No modifica nada: lee Postgres (pipelines/conversaciones), Redis, Meta (campañas) y
hace pings de salud a Chatwoot/Evolution/OpenRouter. Servicio aparte del bot.
"""
import os, time, asyncio, base64, secrets, json
from contextlib import asynccontextmanager

import asyncpg, httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

DB_URL   = os.getenv("DATABASE_URL", "").replace("+asyncpg", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
META_TOKEN = os.getenv("META_TOKEN", "")
CW_URL   = os.getenv("CHATWOOT_URL", "https://chats.example.com")
CW_ACC   = os.getenv("CHATWOOT_ACCOUNT_ID", "11")
CW_TOKEN = os.getenv("CHATWOOT_TOKEN", "")
EVO_URL  = os.getenv("EVOLUTION_URL", "")
EVO_KEY  = os.getenv("EVOLUTION_KEY", "")
EVO_INST = os.getenv("EVOLUTION_INSTANCE", "")
OR_KEY   = os.getenv("OPENROUTER_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "?")
# El bot (servicio aparte) es quien REALMENTE aprueba/rechaza: pone el flag y le manda
# los horarios/Meet al candidato. El dashboard sólo hace de proxy server-side.
# WEBHOOK_SECRET jamás se manda al navegador: se queda aquí y viaja bot↔dashboard.
BOT_INTERNAL_URL = os.getenv("BOT_INTERNAL_URL", "")   # ej. http://172.18.0.1:8090
WEBHOOK_SECRET   = os.getenv("WEBHOOK_SECRET", "")

# Cuentas publicitarias de Meta que vigila el panel (id de cuenta, nombre amigable).
# Reemplaza por tu(s) act_... reales; opcionalmente léelo de una env var.
AD_ACCOUNTS = [
    (os.getenv("AD_ACCOUNT_ID", "act_XXXXXXXXXXXX"),  "Ads reclutamiento"),
]

# Avatares que NO quieres mostrar en el panel (ej. avatares de otro negocio que no es RRHH).
# Deja aquí los slugs a ocultar. El placeholder no calza con nada (no oculta nada por defecto).
EXCLUDED_ROLES = {"__none__"}

# ---- Mapeo campaña → avatar (role_slug) --------------------------------------
# Meta NO nos dice a qué avatar pertenece cada campaña: si todos los adsets promocionan
# el MISMO número de WhatsApp, no se distingue por promoted_object. Solución: resolver por
# PALABRA CLAVE en el nombre de la campaña. La lista está ORDENADA (lo más específico
# primero) y el match es case-insensitive por substring. Devuelve None ("sin avatar") si
# ninguna keyword calza. ⚠️ EJEMPLOS: pon aquí las keywords de TUS campañas → tus slugs.
CAMPAIGN_KEYWORD_ROLE = [
    ("vendedor",   "ejemplo-vendedor"),
    ("ventas",     "ejemplo-vendedor"),
    ("analista",   "ejemplo-reclutador"),
    ("datos",      "ejemplo-reclutador"),
]

def campaign_role(campaign_name: str | None) -> str | None:
    """Devuelve el role_slug del avatar dueño de la campaña, o None si no se reconoce.
    Match por substring case-insensitive contra CAMPAIGN_KEYWORD_ROLE (orden = prioridad).
    OJO: NO usamos la keyword genérica 'asesor' porque calza con varios avatares; las
    campañas 'ASESORAS/ASESORES' viejas y sin marca quedan a propósito como 'sin avatar'."""
    if not campaign_name:
        return None
    n = campaign_name.lower()
    for kw, role in CAMPAIGN_KEYWORD_ROLE:
        if kw in n:
            return role
    return None

_pool: asyncpg.Pool | None = None
_cache: dict = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _pool
    _pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=4)
    yield
    await _pool.close()

app = FastAPI(title="TuEmpresa Recruitment Dashboard", lifespan=lifespan)

# ---- basic-auth (tiene PII de candidatos; se expone por dominio público) ----
DASH_USER = os.getenv("DASH_USER", "admin")
DASH_PASS = os.getenv("DASH_PASS", "")  # si vacío → sin auth (solo dev/localhost)


def _load_users() -> dict:
    """Usuarios del panel: el principal (DASH_USER/DASH_PASS) + extras de DASH_EXTRA_USERS
    con formato 'user1:pass1,user2:pass2'. Todos tienen el MISMO acceso (admin: ven candidatos
    y conversaciones). Así se puede dar acceso propio a RRHH sin compartir la clave del dueño."""
    users = {}
    if DASH_PASS:
        users[DASH_USER] = DASH_PASS
    for pair in (os.getenv("DASH_EXTRA_USERS", "") or "").split(","):
        pair = pair.strip()
        if ":" in pair:
            u, p = pair.split(":", 1)
            if u.strip() and p:
                users[u.strip()] = p
    return users


DASH_USERS = _load_users()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if DASH_USERS:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                u, p = base64.b64decode(hdr[6:]).decode().split(":", 1)
                expected = DASH_USERS.get(u)
                ok = expected is not None and secrets.compare_digest(p, expected)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="app"'})
    return await call_next(request)


async def cached(key: str, ttl: int, coro):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = await coro()
    _cache[key] = (now, val)
    return val


# ---------------- API ----------------
@app.get("/api/overview")
async def overview():
    async with _pool.acquire() as c:
        # excluimos avatares que no son RRHH (otro negocio) → nunca se listan
        rows = await c.fetch("""
            select c.role_slug,
                   count(distinct c.id)                                    as convs,
                   count(m.id) filter (where m.direction='incoming')       as msgs_in,
                   count(m.id) filter (where m.direction='outgoing')       as msgs_out,
                   max(m.created_at)                                       as last_at
            from conversations c left join messages m on m.conversation_id=c.id
            where c.role_slug <> all($1)
            group by c.role_slug order by max(m.created_at) desc nulls last
        """, list(EXCLUDED_ROLES))
        roles = []
        for r in rows:
            roles.append({
                "role": r["role_slug"], "convs": r["convs"],
                "in": r["msgs_in"], "out": r["msgs_out"],
                "last_at": r["last_at"].isoformat() if r["last_at"] else None,
                # señal de alerta: entran mensajes pero el bot no responde
                "stuck": r["msgs_in"] > 0 and r["msgs_out"] == 0,
            })
        tot = await c.fetchrow("""
            select (select count(*) from applicants)    as applicants,
                   (select count(*) from conversations) as conversations,
                   (select count(*) from messages)      as messages
        """)
    return {"roles": roles, "totals": dict(tot)}


@app.get("/api/unanswered")
async def unanswered():
    """Conversaciones donde el ÚLTIMO mensaje es del lead → el bot le debe respuesta."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            with last_msg as (
                select distinct on (conversation_id)
                       conversation_id, direction, content, created_at
                from messages order by conversation_id, id desc
            )
            select c.id, c.role_slug, c.chatwoot_conversation_id,
                   a.name, a.phone, lm.content as last_text, lm.created_at as last_at,
                   extract(epoch from (now()-lm.created_at))/3600 as hours_ago
            from last_msg lm
            join conversations c on c.id=lm.conversation_id
            join applicants a on a.id=c.applicant_id
            where lm.direction='incoming'
            order by lm.created_at desc limit 60
        """)
    out = []
    for r in rows:
        out.append({
            "conv": r["id"], "role": r["role_slug"], "cw": r["chatwoot_conversation_id"],
            "name": r["name"], "phone": r["phone"],
            "last_text": (r["last_text"] or "")[:90],
            "hours_ago": round(r["hours_ago"], 1),
            "in_window": r["hours_ago"] <= 24,
        })
    return {"items": out, "count": len(out)}


# SOLO mostramos campañas que alimentan ESTE bot = las que mandan al WhatsApp
# 51999999999 (Chatwoot cuenta 11). Se detecta por el promoted_object del adset.
BOT_WA = os.getenv("BOT_WHATSAPP", "51999999999")
BOT_WA_DIGITS = "".join(ch for ch in BOT_WA if ch.isdigit())[-9:]

def _camp_row(cp):
    ins = (cp.get("insights", {}) or {}).get("data", [{}])
    ins = ins[0] if ins else {}
    msgs = None
    for a in ins.get("actions", []) or []:
        if "messaging_conversation_started" in a.get("action_type", ""):
            msgs = a.get("value")
    name = cp.get("name")
    return {
        "name": name, "status": cp.get("effective_status"),
        "budget": (int(cp["daily_budget"]) / 100) if cp.get("daily_budget") else None,
        "spend_7d": ins.get("spend"), "msgs_7d": msgs,
        # avatar dueño de la campaña (None = "sin avatar")
        "role": campaign_role(name),
    }

@app.get("/api/successful")
async def successful():
    """Casos donde el agente cumplió su objetivo = consiguió recursos del lead
    (CV/portafolio, fotos, documentos que pidió el agente) o el reclutador lo aprobó.
    Incluye deep-link al chat de Chatwoot y las URLs de los adjuntos recibidos."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            select a.id, c.chatwoot_conversation_id, c.role_slug, a.name, a.phone,
                   a.stage, a.sede, a.eval_score, a.approved_for_interview,
                   a.updated_at,
                   (select coalesce(json_agg(json_build_object(
                        'kind', d.kind, 'file', d.file_name, 'url', coalesce(d.source_url, d.local_path))
                        order by d.id), '[]'::json)
                    from documents d where d.applicant_id=a.id) as docs,
                   (select count(*) from documents d where d.applicant_id=a.id) as ndocs
            from conversations c join applicants a on a.id=c.applicant_id
            where c.role_slug <> all($1)
              and (exists (select 1 from documents d where d.applicant_id=a.id)
               or a.approved_for_interview is true
               or a.stage in ('docs_received','interview_scheduled','done'))
            order by a.updated_at desc nulls last
            limit 50
        """, list(EXCLUDED_ROLES))
    out = []
    for r in rows:
        why = ("aprobado" if r["approved_for_interview"] else
               "docs recibidos" if r["ndocs"] else (r["stage"] or ""))
        out.append({
            "id": r["id"],
            "cw": r["chatwoot_conversation_id"],
            "chat_url": f"{CW_URL}/app/accounts/{CW_ACC}/conversations/{r['chatwoot_conversation_id']}",
            "role": r["role_slug"], "name": r["name"], "phone": r["phone"],
            "stage": r["stage"], "sede": r["sede"], "score": r["eval_score"],
            "approved": r["approved_for_interview"], "why": why,
            "ndocs": r["ndocs"], "docs": json.loads(r["docs"]) if r["docs"] else [],
        })
    return {"items": out, "count": len(out)}


# ---------------- APROBACIÓN DE CANDIDATOS (recruiter decide desde el navegador) ----------------
@app.get("/api/pending")
async def pending(role: str | None = None):
    """Candidatos que esperan la decisión del reclutador: aún sin aprobar/rechazar
    (approved_for_interview IS NULL) PERO que sí mandaron algo (≥1 documento). Se
    excluyen avatares que no son RRHH. Filtro opcional ?role=<slug> para la vista
    por avatar. Incluye deep-link al chat de Chatwoot (igual que /api/successful)."""
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            select a.id, a.name, a.phone, a.role_slug, a.sede, a.eval_score, a.updated_at,
                   (select count(*) from documents d where d.applicant_id=a.id) as ndocs,
                   (select c.chatwoot_conversation_id from conversations c
                     where c.applicant_id=a.id order by c.id desc limit 1) as cw
            from applicants a
            where a.approved_for_interview is null
              and exists (select 1 from documents d where d.applicant_id=a.id)
              and a.role_slug <> all($1)
              and ($2::text is null or a.role_slug=$2)
            order by a.updated_at desc nulls last
            limit 100
        """, list(EXCLUDED_ROLES), role)
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "name": r["name"], "phone": r["phone"],
            "role_slug": r["role_slug"], "sede": r["sede"], "eval_score": r["eval_score"],
            "ndocs": r["ndocs"],
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            "chat_url": (f"{CW_URL}/app/accounts/{CW_ACC}/conversations/{r['cw']}" if r["cw"] else None),
        })
    return {"items": out, "count": len(out)}


async def _bot_decision(action: str, id: int):
    """Proxy server-side hacia el bot: el token viaja aquí, NUNCA al navegador.
    Devuelve el JSON del bot tal cual (propaga ok/error) y garantiza el id en la
    respuesta para que la UI sepa a qué fila corresponde."""
    if not BOT_INTERNAL_URL:
        return {"ok": False, "error": "bot no configurado", "id": id}
    try:
        async with httpx.AsyncClient(timeout=25) as h:
            r = await h.post(f"{BOT_INTERNAL_URL}/internal/{action}/{id}",
                             params={"token": WEBHOOK_SECRET})
        data = r.json()
        if isinstance(data, dict):
            data.setdefault("id", id)
            return data
        return {"ok": False, "error": "respuesta inválida del bot", "id": id}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "id": id}


@app.post("/api/approve/{id}")
async def approve(id: int):
    return await _bot_decision("approve", id)


@app.post("/api/reject/{id}")
async def reject(id: int):
    return await _bot_decision("reject", id)


# ---------------- FICHA DEL CANDIDATO (perfil completo para el reclutador) ----------------
@app.get("/api/candidate/{id}")
async def candidate(id: int):
    """Ficha completa de UN candidato para la página /candidate/{id}. Reúne todo lo que
    el reclutador necesita para decidir sin abrir Chatwoot: datos + eval_notes (la ficha
    [CALIFICA]) + documentos + transcripción de la conversación. 404 si el id no existe."""
    async with _pool.acquire() as c:
        # --- applicant + título humano del agente (join opcional por slug) ---
        a = await c.fetchrow("""
            select a.id, a.name, a.phone, a.role_slug, a.sede, a.eval_score, a.stage,
                   a.approved_for_interview, a.eval_notes, a.created_at, a.updated_at,
                   ag.name as agent_name
            from applicants a
            left join agents ag on ag.slug=a.role_slug
            where a.id=$1
        """, id)
        if not a:
            return JSONResponse({"error": "candidato no existe", "id": id}, status_code=404)

        # --- documentos (reel/CV/fotos): url = source_url si hay, si no local_path ---
        docs = await c.fetch("""
            select kind, file_name, coalesce(source_url, local_path) as url
            from documents where applicant_id=$1 order by id
        """, id)

        # --- conversación: la más reciente del candidato (deep-link a Chatwoot) ---
        cw = await c.fetchval("""
            select chatwoot_conversation_id from conversations
            where applicant_id=$1 order by id desc limit 1
        """, id)

        # --- últimos 40 mensajes de TODAS sus conversaciones, viejo→nuevo (transcripción) ---
        msgs = await c.fetch("""
            select direction, content, created_at from (
                select m.id, m.direction, m.content, m.created_at
                from messages m join conversations c on c.id=m.conversation_id
                where c.applicant_id=$1
                order by m.id desc limit 40
            ) t order by id asc
        """, id)

    phone_digits = "".join(ch for ch in (a["phone"] or "") if ch.isdigit())
    return {
        "applicant": {
            "id": a["id"], "name": a["name"], "phone": a["phone"],
            "role_slug": a["role_slug"], "sede": a["sede"],
            "eval_score": a["eval_score"], "stage": a["stage"],
            "approved_for_interview": a["approved_for_interview"],
            "eval_notes": a["eval_notes"],   # ← la ficha [CALIFICA], lo más importante
            "created_at": a["created_at"].isoformat() if a["created_at"] else None,
            "updated_at": a["updated_at"].isoformat() if a["updated_at"] else None,
        },
        "agent_label": a["agent_name"] or a["role_slug"],
        "documents": [{"kind": d["kind"], "file_name": d["file_name"], "url": d["url"]} for d in docs],
        "conversation": {
            "chatwoot_conversation_id": cw,
            "chat_url": (f"{CW_URL}/app/accounts/{CW_ACC}/conversations/{cw}" if cw else None),
            "wa_url": (f"https://wa.me/{phone_digits}" if phone_digits else None),
        },
        "messages": [
            {"direction": m["direction"], "content": m["content"],
             "created_at": m["created_at"].isoformat() if m["created_at"] else None}
            for m in msgs
        ],
    }


async def _fetch_campaigns():
    if not META_TOKEN:
        return {"error": "sin META_TOKEN"}
    accounts = []
    async with httpx.AsyncClient(timeout=25) as h:
        for act, label in AD_ACCOUNTS:
            try:
                # 1) adsets con promoted_object → ¿qué campañas apuntan a NUESTRO número?
                r = await h.get(f"https://graph.facebook.com/v21.0/{act}/adsets",
                                params={"fields": "campaign_id,promoted_object",
                                        "limit": 400, "access_token": META_TOKEN})
                data = r.json()
                if "error" in data:
                    accounts.append({"account": label, "error": data["error"].get("message", "")[:120]})
                    continue
                cids = set()
                for a in data.get("data", []):
                    po = a.get("promoted_object") or {}
                    wa = "".join(ch for ch in str(po.get("whatsapp_phone_number", "")) if ch.isdigit())
                    if wa and wa.endswith(BOT_WA_DIGITS):
                        cids.add(a.get("campaign_id"))
                if not cids:
                    continue  # esta cuenta no alimenta a este bot → no se muestra
                # 2) traer esas campañas + insights (multi-get por ids)
                rc = await h.get("https://graph.facebook.com/v21.0/",
                                 params={"ids": ",".join(cids),
                                         "fields": "name,effective_status,daily_budget,"
                                                   "insights.date_preset(last_7d){spend,actions}",
                                         "access_token": META_TOKEN})
                cdata = rc.json()
                camps = [_camp_row(cp) for cp in cdata.values() if isinstance(cp, dict) and cp.get("name")]
                # panel SOLO RRHH: descartar campañas de avatares de otro negocio
                camps = [cm for cm in camps if cm.get("role") not in EXCLUDED_ROLES]
                for cm in camps:
                    cm["account"] = label   # etiqueta de la cuenta, útil en la vista por avatar
                camps.sort(key=lambda x: (x["status"] != "ACTIVE", x["name"] or ""))
                accounts.append({"account": label, "campaigns": camps})
            except Exception as e:
                accounts.append({"account": label, "error": str(e)[:120]})
    # "flat" = todas las campañas que alimentan el bot en una sola lista (para la vista por avatar)
    flat = [cm for acc in accounts for cm in acc.get("campaigns", [])]
    return {"accounts": accounts, "flat": flat}


@app.get("/api/campaigns")
async def campaigns():
    # Mantiene la forma histórica {accounts:[...]} para no romper la UI existente.
    data = await cached("campaigns", 300, _fetch_campaigns)
    return {"accounts": data.get("accounts", []), "error": data.get("error")} if "error" in data \
        else {"accounts": data.get("accounts", [])}


# ---------------- VISTA POR AVATAR (role_slug) ----------------
def _f(x):
    """string→float tolerante (los insights de Meta llegan como texto)."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ---- helpers: cuentan UN paso del embudo (una query por paso) ---------------
# Cada helper recibe (conexión, slug) y devuelve un entero. Se reusan entre
# templates para no duplicar SQL.
async def _step_entro(c, slug):
    """Entró = postulantes distintos con este role_slug (base del embudo = 100%)."""
    return await c.fetchval("select count(*) from applicants where role_slug=$1", slug) or 0

async def _step_engaged(c, slug):
    """Enganchó = postulantes cuya conversación tuvo MÁS DE 2 mensajes entrantes
    (chatearon de verdad; clic frío = ≤2 entrantes). Postulantes distintos."""
    return await c.fetchval("""
        select count(*) from (
            select c.applicant_id
            from conversations c
            join messages m on m.conversation_id=c.id and m.direction='incoming'
            where c.role_slug=$1 and c.applicant_id is not null
            group by c.applicant_id
            having count(m.id) > 2
        ) t""", slug) or 0

async def _step_deep_talk(c, slug):
    """Conversó a fondo = postulantes con ≥6 mensajes entrantes (proxy de que dio
    su perfil: capacidad/horizonte). Mismo shape que 'Enganchó' pero con umbral ≥6."""
    return await c.fetchval("""
        select count(*) from (
            select c.applicant_id
            from conversations c
            join messages m on m.conversation_id=c.id and m.direction='incoming'
            where c.role_slug=$1 and c.applicant_id is not null
            group by c.applicant_id
            having count(m.id) >= 6
        ) t""", slug) or 0

async def _step_docs(c, slug):
    """Mandó CV/audio = postulantes distintos con ≥1 documento."""
    return await c.fetchval("""
        select count(distinct d.applicant_id)
        from documents d join applicants a on a.id=d.applicant_id
        where a.role_slug=$1""", slug) or 0

async def _step_approved(c, slug):
    """Pasó el filtro = postulantes aprobados por el reclutador."""
    return await c.fetchval("""
        select count(*) from applicants
        where role_slug=$1 and approved_for_interview is true""", slug) or 0

async def _step_cited(c, slug):
    """Citado = postulantes con ≥1 entrevista NO cancelada / NO ausente."""
    try:
        return await c.fetchval("""
            select count(distinct i.applicant_id)
            from interviews i
            where i.role_slug=$1
              and coalesce(lower(i.status),'') not in ('cancelled','no_show')""", slug) or 0
    except Exception:
        return 0

async def _step_calificado(c, slug):
    """Calificado → reunión = postulantes con stage='calificado'. El bot marca esa
    etapa cuando dispara su marcador [CALIFICA] (es EL filtro del inversionista)."""
    return await c.fetchval("""
        select count(*) from applicants
        where role_slug=$1 and stage='calificado'""", slug) or 0


# ---- TEMPLATES de embudo por avatar -----------------------------------------
# Cada avatar se mide con la TEORÍA de conversión que le corresponde: un rol de
# reclutamiento se mide con CV/audio; un rol de ventas/calificación es otra teoría
# (no hay CV). Un template es una lista ORDENADA de pasos, cada uno
#   (label, emphasis, fn)  donde fn(c, slug) -> count.  emphasis=True → borde dorado.
# Selección: FUNNEL_TEMPLATES.get(slug, RECRUITMENT_DEFAULT).
RECRUITMENT_DEFAULT = [
    ("Entró",          False, _step_entro),
    ("Enganchó",       False, _step_engaged),
    ("Mandó CV/audio", False, _step_docs),
    ("Pasó el filtro", True,  _step_approved),   # dorado: filtro del reclutador
    ("Citado",         True,  _step_cited),      # dorado: entrevista programada
]

# Embudo para roles de VENTAS/CALIFICACIÓN (sin CV; el hito es el marcador [CALIFICA]).
CALIFICACION = [
    ("Entró",                False, _step_entro),
    ("Enganchó",             False, _step_engaged),
    ("Conversó a fondo",     False, _step_deep_talk),
    ("Calificado → reunión", True,  _step_calificado),  # dorado: filtro de ventas
]

# ⚠️ EJEMPLO: mapea aquí los slugs que usen un embudo distinto al de reclutamiento.
FUNNEL_TEMPLATES = {
    "ejemplo-vendedor": CALIFICACION,
}


@app.get("/api/avatar/{slug}")
async def avatar(slug: str):
    """Consolida TODO lo de un avatar: su agente/prompt, su embudo (DB) y sus campañas
    (Meta) con KPIs objetivos (CPL, costo/entrevista, conversión del funnel)."""
    # avatares de otro negocio no pertenecen a este panel de RRHH
    if slug in EXCLUDED_ROLES:
        return JSONResponse({"error": "fuera de alcance (no es RRHH)"}, status_code=404)
    async with _pool.acquire() as c:
        # --- agente (puede no existir → None) ---
        ag = await c.fetchrow("""select name, model, active, length(system_prompt) as plen, tools
                                 from agents where slug=$1""", slug)
        agent = None
        if ag:
            raw = ag["tools"]
            tools = json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
            agent = {"name": ag["name"], "model": ag["model"], "active": ag["active"],
                     "prompt_len": ag["plen"], "tools": tools}

        # --- embudo (DB) ---
        appl = await c.fetchval("select count(*) from applicants where role_slug=$1", slug)
        conv = await c.fetchrow("""
            select count(*) as total,
                   count(*) filter (where created_at > now()-interval '7 days') as d7
            from conversations where role_slug=$1""", slug)
        msgs = await c.fetchrow("""
            select count(m.id) filter (where m.direction='incoming') as msgs_in,
                   count(m.id) filter (where m.direction='outgoing') as msgs_out
            from conversations c join messages m on m.conversation_id=c.id
            where c.role_slug=$1""", slug)
        docs_recv = await c.fetchval("""
            select count(distinct d.applicant_id)
            from documents d join applicants a on a.id=d.applicant_id
            where a.role_slug=$1""", slug)
        approved = await c.fetchval("""
            select count(*) from applicants
            where role_slug=$1 and approved_for_interview is true""", slug)
        # entrevistas programadas = a futuro y no canceladas
        try:
            interviews = await c.fetchval("""
                select count(*) from interviews
                where role_slug=$1
                  and coalesce(lower(status),'') not in ('cancelled','canceled','cancelada')
                  and (scheduled_at is null or scheduled_at > now())""", slug)
        except Exception:
            interviews = None
        stages_rows = await c.fetch("""
            select coalesce(stage,'(sin etapa)') as stage, count(*) as n
            from applicants where role_slug=$1 group by stage order by n desc""", slug)
        stages = [{"stage": r["stage"], "n": r["n"]} for r in stages_rows]

        # --- pasos del EMBUDO DE CONVERSIÓN según el TEMPLATE del avatar ---
        # Cada avatar usa su propia teoría de conversión (reclutamiento vs inversionista).
        # El template define (label, emphasis, fn); aquí ejecutamos cada fn contra la DB.
        _template = FUNNEL_TEMPLATES.get(slug, RECRUITMENT_DEFAULT)
        _step_counts = [(lbl, emph, await fn(c, slug) or 0) for lbl, emph, fn in _template]

    funnel = {
        "applicants": appl or 0,
        "conversations": conv["total"] or 0,
        "conversations_7d": conv["d7"] or 0,
        "msgs_in": msgs["msgs_in"] or 0,
        "msgs_out": msgs["msgs_out"] or 0,
        "docs_received": docs_recv or 0,
        "approved": approved or 0,
        "interviews_scheduled": interviews or 0,
        "stages": stages,
    }

    # --- EMBUDO DE CONVERSIÓN: pasos ordenados del template (base=paso 1=100%) ----
    # Cada paso: label, count, pct_of_total (vs paso 1), pct_of_prev (vs anterior,
    # None en el paso 1 para evitar div/0), dropped_from_prev (cuántos se cayeron) y
    # emphasis (bool → borde dorado, es lo que le importa al dueño de ese avatar).
    _base = _step_counts[0][2] if _step_counts else 0
    funnel_steps = []
    _prev = None
    for _label, _emph, _cnt in _step_counts:
        _cnt = _cnt or 0
        funnel_steps.append({
            "label": _label,
            "count": _cnt,
            "pct_of_total": round(_cnt * 100 / _base, 1) if _base else None,
            "pct_of_prev": round(_cnt * 100 / _prev, 1) if _prev else None,  # guard: None en paso 1
            "dropped_from_prev": (_prev - _cnt) if _prev is not None else None,
            "emphasis": _emph,
        })
        _prev = _cnt

    # --- campañas de este avatar (reutiliza el mismo fetch cacheado que la vista global) ---
    data = await cached("campaigns", 300, _fetch_campaigns)
    camps = [cm for cm in data.get("flat", []) if cm.get("role") == slug]
    camps.sort(key=lambda x: (x["status"] != "ACTIVE", x["name"] or ""))

    # --- KPIs derivados ---
    total_spend = round(sum(_f(cm.get("spend_7d")) or 0 for cm in camps), 2)
    total_msgs  = int(sum(_f(cm.get("msgs_7d")) or 0 for cm in camps))
    def _div(a, b, nd=2):
        return round(a / b, nd) if b else None
    kpis = {
        "total_spend_7d": total_spend,
        "total_msgs_7d": total_msgs,
        # CPL aquí = costo por conversación de WhatsApp iniciada (el "lead" de este funnel)
        "cpl": _div(total_spend, total_msgs),
        "cost_per_interview": _div(total_spend, funnel["interviews_scheduled"]),
        "conv_to_docs_pct": _div(funnel["docs_received"] * 100, funnel["conversations"], 1),
        "docs_to_interview_pct": _div(funnel["interviews_scheduled"] * 100, funnel["docs_received"], 1),
    }
    return {"slug": slug, "agent": agent, "funnel": funnel, "funnel_steps": funnel_steps,
            "campaigns": camps, "kpis": kpis}


async def _fetch_health():
    h = {}
    # postgres
    try:
        async with _pool.acquire() as c:
            await c.fetchval("select 1")
        h["postgres"] = {"ok": True}
    except Exception as e:
        h["postgres"] = {"ok": False, "msg": str(e)[:80]}
    # redis
    try:
        rc = aioredis.from_url(REDIS_URL, decode_responses=True)
        await rc.ping(); await rc.aclose()
        h["redis"] = {"ok": True}
    except Exception as e:
        h["redis"] = {"ok": False, "msg": str(e)[:80]}
    async with httpx.AsyncClient(timeout=12) as cli:
        # chatwoot
        try:
            r = await cli.get(f"{CW_URL}/api/v1/accounts/{CW_ACC}/conversations",
                              params={"per_page": 1}, headers={"api_access_token": CW_TOKEN})
            h["chatwoot"] = {"ok": r.status_code == 200, "msg": f"HTTP {r.status_code}"}
        except Exception as e:
            h["chatwoot"] = {"ok": False, "msg": str(e)[:80]}
        # evolution
        if EVO_URL and EVO_INST:
            try:
                r = await cli.get(f"{EVO_URL}/instance/connectionState/{EVO_INST}",
                                  headers={"apikey": EVO_KEY})
                st = (r.json().get("instance", {}) or {}).get("state") if r.status_code == 200 else None
                h["evolution"] = {"ok": st == "open", "msg": st or f"HTTP {r.status_code}"}
            except Exception as e:
                h["evolution"] = {"ok": False, "msg": str(e)[:80]}
        else:
            h["evolution"] = {"ok": None, "msg": "no configurado"}
        # openrouter
        try:
            r = await cli.get("https://openrouter.ai/api/v1/key",
                              headers={"Authorization": f"Bearer {OR_KEY}"})
            h["openrouter"] = {"ok": r.status_code == 200, "msg": f"modelo {LLM_MODEL}"}
        except Exception as e:
            h["openrouter"] = {"ok": False, "msg": str(e)[:80]}
        # meta token
        try:
            r = await cli.get("https://graph.facebook.com/v21.0/me",
                              params={"access_token": META_TOKEN})
            ok = r.status_code == 200 and "id" in r.json()
            h["meta_token"] = {"ok": ok, "msg": "vivo" if ok else r.json().get("error", {}).get("message", "")[:60]}
        except Exception as e:
            h["meta_token"] = {"ok": False, "msg": str(e)[:80]}
    return h


@app.get("/api/health")
async def health():
    return await cached("health", 60, _fetch_health)


# ---------------- AGENTES (editor) ----------------
@app.get("/api/agents")
async def list_agents():
    async with _pool.acquire() as c:
        rows = await c.fetch("""
            select a.slug, a.name, a.model, a.active, a.updated_at,
                   length(a.system_prompt) as plen, t.slug as tenant
            from agents a left join tenants t on t.id=a.tenant_id
            order by a.active desc, a.slug
        """)
    return {"agents": [dict(r) | {"updated_at": r["updated_at"].isoformat() if r["updated_at"] else None}
                       for r in rows]}


@app.get("/api/tools")
async def tools_catalog():
    try:
        async with _pool.acquire() as c:
            rows = await c.fetch("select name,label,description from tools_catalog order by name")
        return {"tools": [dict(r) for r in rows]}
    except Exception:
        return {"tools": []}


@app.get("/api/agents/{slug}")
async def get_agent(slug: str):
    async with _pool.acquire() as c:
        a = await c.fetchrow("""select id,slug,name,active,model,temperature,system_prompt,
                                       chatwoot_inbox_id,autofill_phrase,tools from agents where slug=$1""", slug)
        if not a:
            return JSONResponse({"error": "no existe"}, status_code=404)
        vs = await c.fetch("""select version,note,created_at,length(system_prompt) as plen
                              from agent_prompt_versions where agent_id=$1 order by version desc limit 20""", a["id"])
    agent = dict(a)
    raw = agent.get("tools")
    agent["tools"] = json.loads(raw) if isinstance(raw, str) else (list(raw) if raw else [])
    return {"agent": agent, "versions": [dict(v) | {"created_at": v["created_at"].isoformat()} for v in vs]}


@app.get("/api/agents/{slug}/version/{version}")
async def get_version(slug: str, version: int):
    async with _pool.acquire() as c:
        sp = await c.fetchval("""select pv.system_prompt from agent_prompt_versions pv
                                 join agents a on a.id=pv.agent_id
                                 where a.slug=$1 and pv.version=$2""", slug, version)
    return {"system_prompt": sp}


@app.put("/api/agents/{slug}")
async def update_agent(slug: str, request: Request):
    data = await request.json()
    async with _pool.acquire() as c:
        row = await c.fetchrow("select id, system_prompt from agents where slug=$1", slug)
        if not row:
            return JSONResponse({"error": "no existe"}, status_code=404)
        new_prompt = data.get("system_prompt")
        changed = new_prompt is not None and new_prompt.strip() and new_prompt != row["system_prompt"]
        await c.execute("""
            update agents set system_prompt=COALESCE($1,system_prompt),
                              model=COALESCE($2,model),
                              active=COALESCE($3,active),
                              temperature=COALESCE($4,temperature),
                              updated_at=now()
            where slug=$5
        """, (new_prompt if changed else None), data.get("model"),
             data.get("active"), data.get("temperature"), slug)
        if isinstance(data.get("tools"), list):
            await c.execute("update agents set tools=$1::jsonb, updated_at=now() where slug=$2",
                            json.dumps(data["tools"]), slug)
        if changed:
            v = await c.fetchval("select COALESCE(max(version),0)+1 from agent_prompt_versions where agent_id=$1", row["id"])
            await c.execute("""insert into agent_prompt_versions(agent_id,version,system_prompt,note)
                               values($1,$2,$3,$4)""", row["id"], v, new_prompt,
                            (data.get("note") or "edición desde panel"))
    return {"ok": True, "prompt_versioned": changed}


# ---------------- UI ----------------
@app.get("/agents", response_class=HTMLResponse)
async def agents_page():
    return AGENTS_HTML
@app.get("/avatar/{slug}", response_class=HTMLResponse)
async def avatar_page(slug: str):
    # una página por avatar; los datos se piden client-side a /api/avatar/{slug}
    # avatares de otro negocio no pertenecen a este panel de RRHH → 404
    if slug in EXCLUDED_ROLES:
        return HTMLResponse("<h1>404</h1><p>No pertenece al panel de RRHH.</p>", status_code=404)
    return AVATAR_HTML.replace("__SLUG__", slug)
@app.get("/candidate/{id}", response_class=HTMLResponse)
async def candidate_page(id: int):
    # ficha completa de un candidato; los datos se piden client-side a /api/candidate/{id}.
    # Chequeo barato de existencia para devolver 404 real si el id no existe.
    async with _pool.acquire() as c:
        exists = await c.fetchval("select 1 from applicants where id=$1", id)
    if not exists:
        return HTMLResponse("<h1>404</h1><p>No existe ese candidato.</p>", status_code=404)
    return CANDIDATE_HTML.replace("__ID__", str(id))
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


HTML = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>TuEmpresa · Panel</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--ink:#e8eaed;--mut:#8b93a1;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#6ea8fe}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px}
header h1{font-size:17px;margin:0;font-weight:650}.sub{color:var(--mut);font-size:12px}
.wrap{padding:20px 24px;max-width:1200px;margin:0 auto}
.grid{display:grid;gap:16px}.g4{grid-template-columns:repeat(4,1fr)}.g2{grid-template-columns:1.3fr 1fr}
@media(max-width:900px){.g4{grid-template-columns:1fr 1fr}.g2{grid-template-columns:1fr}}
@media(max-width:640px){
 header{padding:13px 15px;flex-wrap:wrap;gap:6px 12px}header h1{font-size:16px;flex:1 0 100%}
 .wrap{padding:13px 13px}.grid{gap:12px}.kpi{font-size:21px}.card{padding:13px}.refresh{margin-left:0}
 #roles,#unans,#exitos,#camps,#pend{overflow-x:auto;-webkit-overflow-scrolling:touch}
 #roles table,#unans table,#exitos table,#camps table,#pend table{min-width:500px}
}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 12px}
.kpi{font-size:26px;font-weight:700}.kpi small{font-size:12px;color:var(--mut);font-weight:400}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600}
.pok{background:rgba(63,185,80,.15);color:var(--ok)}.pbad{background:rgba(248,81,73,.15);color:var(--bad)}
.pwarn{background:rgba(210,153,34,.15);color:var(--warn)}.pmut{background:#222733;color:var(--mut)}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px}
.right{text-align:right}.mut{color:var(--mut)}.b{font-weight:600}
a{color:var(--acc);text-decoration:none}.refresh{margin-left:auto;color:var(--mut);font-size:12px}
/* botones de decisión del reclutador (aprobar / rechazar) */
.btn{border:0;border-radius:7px;padding:5px 11px;font-weight:700;font-size:12px;cursor:pointer;margin-right:5px}
.btn.ok{background:rgba(63,185,80,.2);color:var(--ok)}.btn.bad{background:rgba(248,81,73,.18);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:default}.dmsg{font-size:12px}
</style></head><body>
<header><h1>🟢 TuEmpresa · Panel</h1>
<a href="/" class=b>Resumen</a><a href="/agents">⚙️ Agentes</a>
<span class=sub id=clock></span><span class=refresh id=rf>cargando…</span></header>
<div class=wrap>
 <div class="grid g4" id=kpis></div>
 <div style="height:16px"></div>
 <div class=card><h2>🗳️ Pendientes de aprobar · decide aquí (le llegan horarios/Meet al aprobar)</h2><div id=pend>…</div></div>
 <div style="height:16px"></div>
 <div class="grid g2">
   <div class=card><h2>Salud de servicios</h2><div id=health>…</div></div>
   <div class=card><h2>Leads sin responder (el bot debe contestar)</h2><div id=unans>…</div></div>
 </div>
 <div style="height:16px"></div>
 <div class=card><h2>Pipeline por rol / campaña</h2><div id=roles>…</div></div>
 <div style="height:16px"></div>
 <div class=card><h2>✅ Casos exitosos · objetivo cumplido (recursos recibidos) — clic para abrir el chat</h2><div id=exitos>…</div></div>
 <div style="height:16px"></div>
 <div class=card><h2>Campañas que alimentan este bot · WhatsApp 51999999999 (cuenta 11) · últimos 7 días</h2><div id=camps>…</div></div>
</div>
<script>
const $=id=>document.getElementById(id);
function ago(iso){if(!iso)return '—';const h=(Date.now()-new Date(iso))/36e5;return h<1?Math.round(h*60)+'m':h<48?h.toFixed(0)+'h':Math.round(h/24)+'d';}
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
async function sect(id,fn){try{await fn();}catch(e){if($(id))$(id).innerHTML='<span class=mut>no disponible ('+e+')</span>';}}
async function load(){
 await Promise.all([
  sect('roles', loadOverview), sect('unans', loadUnans), sect('health', loadHealth),
  sect('exitos', loadExitos), sect('camps', loadCamps), sect('pend', loadPending),
 ]);
 $('rf').textContent='actualizado '+new Date().toLocaleTimeString('es-PE');
}
async function loadOverview(){
  const ov=await j('/api/overview');
  $('kpis').innerHTML=[
   ['Postulantes',ov.totals.applicants],['Conversaciones',ov.totals.conversations],
   ['Mensajes',ov.totals.messages],['Roles activos',ov.roles.length]
  ].map(k=>`<div class=card><h2>${k[0]}</h2><div class=kpi>${k[1]}</div></div>`).join('');
  $('roles').innerHTML='<table><tr><th>Rol</th><th class=right>Conv</th><th class=right>Entran</th><th class=right>Responde</th><th class=right>Último</th><th></th></tr>'+
   ov.roles.map(r=>`<tr><td class=b><a href="/avatar/${encodeURIComponent(r.role)}">${r.role} ↗</a></td><td class=right>${r.convs}</td><td class=right>${r.in}</td>
   <td class=right>${r.out}</td><td class="right mut">${ago(r.last_at)}</td>
   <td>${r.stuck?'<span class="pill pbad">SIN RESPONDER</span>':''}</td></tr>`).join('')+'</table>';
}
async function loadUnans(){
  const un=await j('/api/unanswered');
  $('unans').innerHTML=un.count? '<table><tr><th>Rol</th><th>Nombre</th><th>Último mensaje</th><th class=right>Hace</th></tr>'+
   un.items.slice(0,12).map(x=>`<tr><td>${x.role}</td><td class=b>${x.name||x.phone||'—'}</td>
   <td class=mut>${(x.last_text||'').replace(/</g,'&lt;')}</td>
   <td class=right><span class="pill ${x.in_window?'pwarn':'pmut'}">${x.hours_ago}h</span></td></tr>`).join('')+'</table>'
   : '<div class=mut>Nada pendiente 🎉</div>';
}
async function loadHealth(){
  const hl=await j('/api/health');
  $('health').innerHTML=Object.entries(hl).map(([k,v])=>{
   const c=v.ok===true?'var(--ok)':v.ok===false?'var(--bad)':'var(--mut)';
   return `<div style="display:flex;align-items:center;padding:5px 0"><span class=dot style="background:${c}"></span>
   <span class=b style="width:120px">${k}</span><span class=mut>${v.msg||''}</span></div>`;}).join('');
}
async function loadExitos(){
  const dicon=k=>(k==='cv'||k==='portfolio'||k==='file')?'📄':(k==='image'?'🖼️':(k==='video'?'🎬':(k==='audio'?'🎧':'📎')));
  const ex=await j('/api/successful');
  $('exitos').innerHTML=ex.count? '<table><tr><th>Agente</th><th>Lead</th><th>Estado</th><th>Recursos recibidos</th><th></th></tr>'+
   ex.items.slice(0,20).map(x=>{
     const docs=(x.docs||[]).filter(d=>d.url).map(d=>`<a href="${d.url}" target=_blank>${dicon(d.kind)} ${(d.file||d.kind||'').slice(0,22)}</a>`).join(' · ')||'<span class=mut>—</span>';
     return `<tr><td>${x.role}</td><td class=b>${x.id?`<a href="/candidate/${x.id}">${x.name||x.phone||'—'} ↗</a>`:(x.name||x.phone||'—')}</td>
      <td><span class="pill ${x.approved?'pok':'pwarn'}">${x.why}</span> ${x.sede?'· '+x.sede:''}${x.score!=null?' · ★'+x.score:''}</td>
      <td>${docs}</td>
      <td><a href="${x.chat_url}" target=_blank class=b>abrir chat ↗</a></td></tr>`;}).join('')+'</table>'
   : '<div class=mut>aún no hay casos con recursos recibidos</div>';
}
async function loadCamps(){
  const cp=await j('/api/campaigns');
  $('camps').innerHTML=(cp.accounts||[]).map(a=>{
   if(a.error)return `<div class=b>${a.account}</div><div class="pill pbad">${a.error}</div><br>`;
   const rows=(a.campaigns||[]).filter(c=>c.status==='ACTIVE'||c.spend_7d).slice(0,8).map(c=>
    `<tr><td>${(c.name||'').replace(/</g,'&lt;')}</td>
     <td><span class="pill ${c.status==='ACTIVE'?'pok':'pmut'}">${c.status}</span></td>
     <td class=right>${c.budget?'$'+c.budget:'—'}</td>
     <td class=right>${c.spend_7d?'$'+c.spend_7d:'—'}</td>
     <td class=right>${c.msgs_7d||'—'}</td></tr>`).join('');
   return `<div class=b style="margin:10px 0 4px">${a.account}</div>
    <table><tr><th>Campaña</th><th>Estado</th><th class=right>$/día</th><th class=right>Gasto 7d</th><th class=right>Convos</th></tr>${rows||'<tr><td class=mut>sin campañas activas</td></tr>'}</table>`;
  }).join('');
}
// ---- Pendientes de aprobar: lista + botones aprobar/rechazar ----
function rowPending(x){
  return `<tr id="pend-${x.id}"><td>${x.role_slug||'—'}</td><td class=b><a href="/candidate/${x.id}">${(x.name||x.phone||'—')} ↗</a></td>
   <td class=right>${x.eval_score!=null?'★'+x.eval_score:'—'}</td>
   <td class=right>${x.ndocs}</td>
   <td>${x.chat_url?`<a href="${x.chat_url}" target=_blank class=b>abrir ↗</a>`:'<span class=mut>—</span>'}</td>
   <td style="white-space:nowrap"><button class="btn ok" onclick="decide(${x.id},'approve',this)">✅ Aprobar</button><button class="btn bad" onclick="decide(${x.id},'reject',this)">❌ Rechazar</button><span class=dmsg></span></td></tr>`;
}
async function loadPending(){
  const p=await j('/api/pending');
  $('pend').innerHTML=p.count? '<table><tr><th>Rol</th><th>Candidato</th><th class=right>Score</th><th class=right>Docs</th><th>Chat</th><th>Decisión</th></tr>'+
    p.items.map(rowPending).join('')+'</table>'
   : '<div class=mut>nada pendiente de aprobar 🎉</div>';
}
// Llama al proxy server-side; el token nunca toca el navegador. Bloquea botones en vuelo.
async function decide(id,action,btn){
  const row=$('pend-'+id); if(!row)return;
  const btns=row.querySelectorAll('button'); btns.forEach(b=>b.disabled=true);
  const msg=row.querySelector('.dmsg'); msg.textContent=' …';
  try{
    const r=await fetch('/api/'+action+'/'+id,{method:'POST'});
    const d=await r.json();
    if(d&&d.ok){
      msg.innerHTML=action==='approve'?' <span class="pill pok">✅ aprobado — se le ofrecieron horarios</span>':' <span class="pill pbad">❌ rechazado</span>';
      setTimeout(()=>row.remove(),2600);
    }else{
      msg.innerHTML=' <span class="pill pbad">'+((d&&d.error)||'error')+'</span>'; btns.forEach(b=>b.disabled=false);
    }
  }catch(e){ msg.innerHTML=' <span class="pill pbad">'+e+'</span>'; btns.forEach(b=>b.disabled=false); }
}
setInterval(()=>{$('clock').textContent=new Date().toLocaleString('es-PE');},1000);
load();setInterval(load,30000);
</script></body></html>"""


AGENTS_HTML = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>TuEmpresa · Agentes</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--ink:#e8eaed;--mut:#8b93a1;--ok:#3fb950;--bad:#f85149;--acc:#6ea8fe}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:16px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px}
header h1{font-size:17px;margin:0}a{color:var(--acc);text-decoration:none}.b{font-weight:600}
.wrap{display:grid;grid-template-columns:300px 1fr;gap:0;height:calc(100vh - 58px)}
.list{border-right:1px solid var(--line);overflow:auto}
@media(max-width:640px){
 header{padding:13px 15px}header h1{font-size:16px}
 .wrap{grid-template-columns:1fr;height:auto}
 .list{border-right:0;border-bottom:1px solid var(--line);max-height:38vh}
 .edit{padding:16px 15px}textarea{min-height:260px}
 .row{flex-wrap:wrap}.row>div{flex:1 0 100%}
}
.item{padding:11px 18px;border-bottom:1px solid var(--line);cursor:pointer}
.item:hover{background:#1c2029}.item.sel{background:#1f2530;border-left:3px solid var(--acc)}
.item .nm{font-weight:600}.item .meta{color:var(--mut);font-size:12px}
.off{opacity:.5}.edit{padding:20px 26px;overflow:auto}
label{display:block;color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin:14px 0 5px}
input,textarea,select{width:100%;background:#10131a;border:1px solid var(--line);color:var(--ink);border-radius:8px;padding:9px;font:inherit}
textarea{min-height:340px;font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.5;resize:vertical}
.row{display:flex;gap:14px;align-items:center}.row>div{flex:1}
button{background:var(--acc);color:#06122b;border:0;border-radius:8px;padding:10px 20px;font-weight:700;cursor:pointer}
button.ghost{background:#222733;color:var(--ink)}.pill{padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;background:#222733;color:var(--mut)}
.vers{font-size:12px}.vers div{padding:6px 0;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.muted{color:var(--mut)}#msg{margin-left:12px}
</style></head><body>
<header><h1>⚙️ TuEmpresa · Agentes</h1><a href="/" class=b>← Resumen</a>
<span id=msg class=muted></span></header>
<div class=wrap>
 <div class=list id=list>cargando…</div>
 <div class=edit id=edit><div class=muted>← elige un agente para editar su prompt</div></div>
</div>
<script>
const $=s=>document.querySelector(s); let cur=null, TOOLS=[];
async function j(u,o){const r=await fetch(u,o);return r.json();}
async function loadList(){
 if(!TOOLS.length){try{TOOLS=(await j('/api/tools')).tools||[]}catch(e){}}
 const d=await j('/api/agents');
 $('#list').innerHTML=d.agents.map(a=>`<div class="item ${a.active?'':'off'}" data-s="${a.slug}" onclick="sel('${a.slug}')">
   <div class=nm>${a.active?'●':'○'} ${a.name}</div>
   <div class=meta>${a.slug} · ${a.model.split('/').pop()} · ${a.plen} chars</div></div>`).join('');
}
async function sel(slug){
 cur=slug; document.querySelectorAll('.item').forEach(e=>e.classList.toggle('sel',e.dataset.s===slug));
 const d=await j('/api/agents/'+slug); const a=d.agent;
 $('#edit').innerHTML=`
  <div class=row><div><h2 style="margin:0">${a.name}</h2><span class=pill>${a.slug}</span></div>
    <div style="flex:0"><label>Activo</label><input type=checkbox id=active ${a.active?'checked':''}></div></div>
  <div class=row>
    <div><label>Modelo</label><input id=model value="${a.model}"></div>
    <div style="flex:0;min-width:120px"><label>Temp.</label><input id=temp value="${a.temperature??''}"></div>
  </div>
  <label>Tools (capacidades — datos duros que no fallan)</label>
  <div id=tools>${(TOOLS.length?TOOLS:[]).map(t=>`<label style="text-transform:none;letter-spacing:0;color:var(--ink);display:flex;gap:8px;align-items:flex-start;margin:6px 0">
     <input type=checkbox class=tool value="${t.name}" ${(a.tools||[]).includes(t.name)?'checked':''}>
     <span><b>${t.label}</b><span class=muted style="display:block;font-size:12px">${(t.description||'').replace(/</g,'&lt;')}</span></span></label>`).join('')||'<span class=muted>sin tools en el catálogo</span>'}</div>
  <label>System prompt</label><textarea id=prompt>${(a.system_prompt||'').replace(/</g,'&lt;')}</textarea>
  <label>Nota del cambio (opcional)</label><input id=note placeholder="por qué editas">
  <div style="margin-top:14px"><button onclick="save()">💾 Guardar (nueva versión)</button>
    <span id=saved class=muted></span></div>
  <label style="margin-top:22px">Historial de versiones</label>
  <div class=vers>${d.versions.map(v=>`<div><span>v${v.version} · ${v.plen} chars · <span class=muted>${(v.note||'')}</span></span>
     <a href="#" onclick="restore('${slug}',${v.version});return false">restaurar</a></div>`).join('')||'<span class=muted>sin versiones</span>'}</div>`;
}
async function restore(slug,v){
 const d=await j(`/api/agents/${slug}/version/${v}`);
 $('#prompt').value=d.system_prompt; $('#saved').textContent='cargado v'+v+' (revisa y guarda)';
}
async function save(){
 const tools=[...document.querySelectorAll('.tool:checked')].map(e=>e.value);
 const body={system_prompt:$('#prompt').value, model:$('#model').value,
   active:$('#active').checked, temperature:parseFloat($('#temp').value)||null, note:$('#note').value, tools};
 const r=await j('/api/agents/'+cur,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 $('#saved').textContent=r.ok?('✅ guardado'+(r.prompt_versioned?' (nueva versión)':'')):'error';
 loadList(); if(r.prompt_versioned) sel(cur);
}
loadList();
</script></body></html>"""


# ---------------- UI: página consolidada por AVATAR ----------------
# __SLUG__ se reemplaza en el handler; todo lo demás se pide a /api/avatar/{slug}.
AVATAR_HTML = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>TuEmpresa · Avatar</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--ink:#e8eaed;--mut:#8b93a1;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#6ea8fe;--gold:#e3b341}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:650}a{color:var(--acc);text-decoration:none}.b{font-weight:600}
.sub{color:var(--mut);font-size:12px}
.wrap{padding:20px 24px;max-width:1100px;margin:0 auto}
.grid{display:grid;gap:16px}.g5{grid-template-columns:repeat(5,1fr)}.g2{grid-template-columns:1fr 1fr}
@media(max-width:900px){.g5{grid-template-columns:repeat(2,1fr)}.g2{grid-template-columns:1fr}}
@media(max-width:640px){
 header{padding:13px 15px;gap:6px 12px}header h1{font-size:16px;flex:1 0 100%}
 .wrap{padding:13px 13px}.grid{gap:12px}.kpi{font-size:20px}.card{padding:13px}
 #camps,#pend{overflow-x:auto;-webkit-overflow-scrolling:touch}#camps table,#pend table{min-width:520px}
}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 12px}
.kpi{font-size:24px;font-weight:700}.kpi small{font-size:12px;color:var(--mut);font-weight:400}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600}
.pok{background:rgba(63,185,80,.15);color:var(--ok)}.pmut{background:#222733;color:var(--mut)}
.pbad{background:rgba(248,81,73,.15);color:var(--bad)}
.right{text-align:right}.mut{color:var(--mut)}.b{font-weight:600}
/* botones de decisión del reclutador (aprobar / rechazar) dentro de la vista del avatar */
.btn{border:0;border-radius:7px;padding:5px 11px;font-weight:700;font-size:12px;cursor:pointer;margin-right:5px}
.btn.ok{background:rgba(63,185,80,.2);color:var(--ok)}.btn.bad{background:rgba(248,81,73,.18);color:var(--bad)}
.btn:disabled{opacity:.45;cursor:default}.dmsg{font-size:12px}
.fun{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--line)}
/* embudo de conversión: barras horizontales proporcionales al % del total */
.fstep{margin:9px 0}
.flabel{display:flex;justify-content:space-between;gap:8px;font-size:12px;margin-bottom:4px}
.flabel .fnum{color:var(--gold);font-weight:700;margin-right:5px}
.fbar-wrap{background:#10131a;border:1px solid var(--line);border-radius:8px;padding:2px}
.fbar{min-width:34px;padding:7px 10px;border-radius:6px;font-weight:700;font-size:12px;white-space:nowrap;
      background:linear-gradient(90deg,rgba(110,168,254,.42),rgba(110,168,254,.14));transition:width .4s}
/* etapas clave (filtro / citado): resaltadas en dorado, es lo que le importa al dueño */
.fstep.key .fbar-wrap{border-color:var(--gold);box-shadow:0 0 0 1px rgba(227,179,65,.25)}
.fstep.key .fbar{background:linear-gradient(90deg,rgba(227,179,65,.5),rgba(227,179,65,.16))}
.fdrop{color:var(--mut);font-size:11px;text-align:center;padding:1px 0}
</style></head><body>
<header><h1>👤 <span id=aname>…</span></h1>
<span class=sub id=asub></span>
<span style="margin-left:auto"><a href="/" class=b>← Resumen</a> · <a href="/agents">⚙️ Agentes</a></span></header>
<div class=wrap>
 <div class="grid g5" id=kpis></div>
 <div style="height:16px"></div>
 <div class=card><h2>🗳️ Pendientes de aprobar · decide aquí (le llegan horarios/Meet al aprobar)</h2><div id=pend>…</div></div>
 <div style="height:16px"></div>
 <div class=card><h2>Embudo de conversión</h2><div id=funsteps>…</div></div>
 <div style="height:16px"></div>
 <div class="grid g2">
   <div class=card><h2>Embudo (detalle)</h2><div id=funnel>…</div></div>
   <div class=card><h2>Etapas del pipeline</h2><div id=stages>…</div></div>
 </div>
 <div style="height:16px"></div>
 <div class=card id=camps><h2>Campañas de este avatar · últimos 7 días</h2><div id=campsbody>…</div></div>
</div>
<script>
const SLUG="__SLUG__";const $=id=>document.getElementById(id);
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
const money=v=>(v==null?'—':'$'+Number(v).toLocaleString('en-US',{maximumFractionDigits:2}));
const pct=v=>(v==null?'—':v+'%');
function kpi(t,v){return `<div class=card><h2>${t}</h2><div class=kpi>${v}</div></div>`;}
// dibuja el embudo de 5 pasos: cada barra con ancho ∝ % del total; entre pasos, la caída
function funnelHtml(steps){
  if(!steps||!steps.length) return '<div class=mut>sin datos</div>';
  let h='';
  steps.forEach((s,i)=>{
    if(i>0 && s.dropped_from_prev!=null && s.dropped_from_prev>0){
      const dp=s.pct_of_prev!=null?(100-s.pct_of_prev).toFixed(1):null;
      h+=`<div class=fdrop>▼ −${s.dropped_from_prev}${dp!=null?' (−'+dp+'%)':''}</div>`;
    }
    const w=Math.max(s.pct_of_total||0,2);           // mínimo visible aunque sea 0%
    const isk=!!s.emphasis;                            // etapa clave → resaltado dorado (viene del template)
    const prevtxt=(s.pct_of_prev!=null)?` · <span class=mut>${s.pct_of_prev}% vs. anterior</span>`:'';
    h+=`<div class="fstep${isk?' key':''}">
      <div class=flabel>
        <span><span class=fnum>${i+1}</span>${s.label}</span>
        <span><span class=b>${s.count}</span> · ${s.pct_of_total!=null?s.pct_of_total+'%':'—'}${prevtxt}</span>
      </div>
      <div class=fbar-wrap><div class=fbar style="width:${w}%">${s.count}</div></div>
    </div>`;
  });
  return h;
}
async function load(){
 const d=await j('/api/avatar/'+encodeURIComponent(SLUG));
 const ag=d.agent, f=d.funnel, k=d.kpis;
 $('aname').textContent=(ag&&ag.name)?ag.name:SLUG;
 $('asub').innerHTML=`${SLUG}${ag?` · ${(ag.model||'').split('/').pop()} · `+(ag.active?'<span class="pill pok">activo</span>':'<span class="pill pmut">inactivo</span>')+` · prompt ${ag.prompt_len} chars`:' · <span class=mut>sin agente</span>'}`;
 $('kpis').innerHTML=[
   kpi('Gasto 7d',money(k.total_spend_7d)),
   kpi('CPL (por convo)',money(k.cpl)),
   kpi('Convos WA 7d',k.total_msgs_7d),
   kpi('Entrevistas',f.interviews_scheduled),
   kpi('Costo/entrevista',money(k.cost_per_interview)),
 ].join('');
 $('funsteps').innerHTML=funnelHtml(d.funnel_steps);
 const fr=(l,v)=>`<div class=fun><span class=mut>${l}</span><span class=b>${v}</span></div>`;
 $('funnel').innerHTML=
   fr('Postulantes',f.applicants)+fr('Conversaciones',f.conversations)+
   fr('Conversaciones (7d)',f.conversations_7d)+
   fr('Mensajes entran / responde',f.msgs_in+' / '+f.msgs_out)+
   fr('Docs recibidos (leads con ≥1)',f.docs_received)+
   fr('Aprobados p/ entrevista',f.approved)+
   fr('Entrevistas programadas',f.interviews_scheduled)+
   fr('Conv→Docs',pct(k.conv_to_docs_pct))+fr('Docs→Entrevista',pct(k.docs_to_interview_pct));
 $('stages').innerHTML=(f.stages&&f.stages.length)?
   '<table><tr><th>Etapa</th><th class=right>N°</th></tr>'+
   f.stages.map(s=>`<tr><td>${s.stage}</td><td class=right>${s.n}</td></tr>`).join('')+'</table>'
   :'<div class=mut>sin datos</div>';
 const rows=(d.campaigns||[]).map(c=>
   `<tr><td>${(c.name||'').replace(/</g,'&lt;')}</td>
    <td class=mut>${c.account||''}</td>
    <td><span class="pill ${c.status==='ACTIVE'?'pok':'pmut'}">${c.status}</span></td>
    <td class=right>${c.budget?'$'+c.budget:'—'}</td>
    <td class=right>${money(c.spend_7d)}</td>
    <td class=right>${c.msgs_7d||'—'}</td></tr>`).join('');
 $('campsbody').innerHTML=rows?
   '<table><tr><th>Campaña</th><th>Cuenta</th><th>Estado</th><th class=right>$/día</th><th class=right>Gasto 7d</th><th class=right>Convos</th></tr>'+rows+'</table>'
   :'<div class=mut>sin campañas asignadas a este avatar</div>';
}
// ---- Pendientes de aprobar (filtrados por este avatar) ----
function rowPending(x){
  return `<tr id="pend-${x.id}"><td class=b><a href="/candidate/${x.id}">${(x.name||x.phone||'—')} ↗</a></td>
   <td class=right>${x.eval_score!=null?'★'+x.eval_score:'—'}</td>
   <td class=right>${x.ndocs}</td>
   <td>${x.chat_url?`<a href="${x.chat_url}" target=_blank class=b>abrir ↗</a>`:'<span class=mut>—</span>'}</td>
   <td style="white-space:nowrap"><button class="btn ok" onclick="decide(${x.id},'approve',this)">✅ Aprobar</button><button class="btn bad" onclick="decide(${x.id},'reject',this)">❌ Rechazar</button><span class=dmsg></span></td></tr>`;
}
async function loadPending(){
  const p=await j('/api/pending?role='+encodeURIComponent(SLUG));
  $('pend').innerHTML=p.count? '<table><tr><th>Candidato</th><th class=right>Score</th><th class=right>Docs</th><th>Chat</th><th>Decisión</th></tr>'+
    p.items.map(rowPending).join('')+'</table>'
   : '<div class=mut>nada pendiente de aprobar 🎉</div>';
}
// Llama al proxy server-side; el token nunca toca el navegador. Bloquea botones en vuelo.
async function decide(id,action,btn){
  const row=$('pend-'+id); if(!row)return;
  const btns=row.querySelectorAll('button'); btns.forEach(b=>b.disabled=true);
  const msg=row.querySelector('.dmsg'); msg.textContent=' …';
  try{
    const r=await fetch('/api/'+action+'/'+id,{method:'POST'});
    const d=await r.json();
    if(d&&d.ok){
      msg.innerHTML=action==='approve'?' <span class="pill pok">✅ aprobado — se le ofrecieron horarios</span>':' <span class="pill pbad">❌ rechazado</span>';
      setTimeout(()=>row.remove(),2600);
    }else{
      msg.innerHTML=' <span class="pill pbad">'+((d&&d.error)||'error')+'</span>'; btns.forEach(b=>b.disabled=false);
    }
  }catch(e){ msg.innerHTML=' <span class="pill pbad">'+e+'</span>'; btns.forEach(b=>b.disabled=false); }
}
load().catch(e=>{$('funsteps').innerHTML=$('funnel').innerHTML='<span class=mut>no disponible ('+e+')</span>';});
sect('pend',loadPending);
setInterval(()=>{load().catch(()=>{});sect('pend',loadPending);},30000);
function sect(id,fn){fn().catch(e=>{if($(id))$(id).innerHTML='<span class=mut>no disponible ('+e+')</span>';});}
</script></body></html>"""


# ---------------- UI: FICHA COMPLETA DEL CANDIDATO ----------------
# __ID__ se reemplaza en el handler; todo lo demás se pide a /api/candidate/{id}.
# Es la página que abre el reclutador desde el link del WhatsApp de alerta.
CANDIDATE_HTML = """<!doctype html><html lang=es><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>TuEmpresa · Candidato</title>
<style>
:root{--bg:#0f1115;--card:#171a21;--line:#252a34;--ink:#e8eaed;--mut:#8b93a1;--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#6ea8fe;--gold:#e3b341}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:650}a{color:var(--acc);text-decoration:none}.b{font-weight:600}
.sub{color:var(--mut);font-size:12px}
.wrap{padding:20px 24px;max-width:920px;margin:0 auto}
.grid{display:grid;gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:0 0 12px}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600}
.pok{background:rgba(63,185,80,.15);color:var(--ok)}.pmut{background:#222733;color:var(--mut)}
.pbad{background:rgba(248,81,73,.15);color:var(--bad)}.pwarn{background:rgba(210,153,34,.15);color:var(--warn)}
.mut{color:var(--mut)}.b{font-weight:600}
/* cabecera del candidato: nombre + chips de contexto */
.hgrid{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px}
.hname{font-size:22px;font-weight:700}
.chip{background:#10131a;border:1px solid var(--line);border-radius:20px;padding:3px 11px;font-size:12px;color:var(--mut)}
.score{background:linear-gradient(90deg,rgba(227,179,65,.5),rgba(227,179,65,.16));border:1px solid var(--gold);
       color:var(--gold);border-radius:10px;padding:5px 12px;font-weight:800;font-size:16px}
/* ficha [CALIFICA]: texto plano, respeta saltos de línea */
.ficha{white-space:pre-wrap;font-size:14px;line-height:1.6;background:#10131a;border:1px solid var(--line);border-radius:10px;padding:14px}
/* documentos */
.docs a{display:inline-flex;align-items:center;gap:7px;background:#10131a;border:1px solid var(--line);
        border-radius:10px;padding:8px 12px;margin:4px 6px 4px 0;color:var(--ink)}
.docs a:hover{border-color:var(--acc)}.docs .k{color:var(--mut);font-size:12px}
/* transcripción tipo chat */
.chat{display:flex;flex-direction:column;gap:8px;max-height:520px;overflow-y:auto}
.bub{max-width:78%;padding:8px 11px;border-radius:12px;font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.bin{align-self:flex-start;background:#1c2029;border:1px solid var(--line);border-bottom-left-radius:3px}
.bout{align-self:flex-end;background:rgba(110,168,254,.16);border:1px solid rgba(110,168,254,.28);border-bottom-right-radius:3px}
.bts{display:block;color:var(--mut);font-size:10px;margin-top:3px}
/* acciones */
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.btn{border:0;border-radius:8px;padding:9px 15px;font-weight:700;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}
.btn.ok{background:rgba(63,185,80,.2);color:var(--ok)}.btn.bad{background:rgba(248,81,73,.18);color:var(--bad)}
.btn.ghost{background:#222733;color:var(--ink)}.btn:disabled{opacity:.45;cursor:default}
@media(max-width:640px){
 header{padding:13px 15px;gap:6px 12px}header h1{font-size:16px;flex:1 0 100%}
 .wrap{padding:13px 13px}.grid{gap:12px}.card{padding:13px}.hname{font-size:19px}
 .bub{max-width:88%}.chat{max-height:60vh}
}
</style></head><body>
<header><h1>👤 Ficha del candidato</h1>
<span style="margin-left:auto"><a href="/" class=b>← Volver</a> · <a href="/agents">⚙️ Agentes</a></span></header>
<div class="wrap grid" id=root><div class=card><div class=mut>cargando…</div></div></div>
<script>
const ID="__ID__";const $=id=>document.getElementById(id);
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[ch]));
function ts(iso){if(!iso)return '';const d=new Date(iso);return d.toLocaleString('es-PE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}
const dicon=k=>(k==='cv'||k==='portfolio'||k==='file')?'📄':(k==='image'?'🖼️':(k==='video'||k==='reel')?'🎬':(k==='audio'?'🎧':'📎'));
async function load(){
 let d;
 try{
   const r=await fetch('/api/candidate/'+ID);
   if(r.status===404){$('root').innerHTML='<div class=card><h2>Candidato</h2><div class=mut>No existe el candidato #'+esc(ID)+'.</div></div>';return;}
   if(!r.ok)throw new Error('HTTP '+r.status);
   d=await r.json();
 }catch(e){$('root').innerHTML='<div class=card><div class=mut>no disponible ('+esc(e)+')</div></div>';return;}
 const a=d.applicant, conv=d.conversation||{};
 // estado de aprobación → pill
 const ap=a.approved_for_interview;
 const appPill=ap===true?'<span class="pill pok">Aprobado</span>':ap===false?'<span class="pill pbad">Rechazado</span>':'<span class="pill pwarn">Pendiente</span>';
 const waHref=conv.wa_url||(a.phone?('https://wa.me/'+a.phone.replace(/[^0-9]/g,'')):null);
 // --- cabecera ---
 let h=`<div class=card>
   <div class=hgrid>
     <span class=hname>${esc(a.name||'(sin nombre)')}</span>
     ${a.eval_score!=null?`<span class=score>★ ${esc(a.eval_score)}</span>`:''}
     ${appPill}
   </div>
   <div class=hgrid style="margin-top:11px">
     <span class=chip>🎭 ${esc(d.agent_label||a.role_slug||'—')}</span>
     ${a.sede?`<span class=chip>📍 ${esc(a.sede)}</span>`:''}
     ${a.stage?`<span class=chip>🧭 ${esc(a.stage)}</span>`:''}
     ${a.phone?`<span class=chip>📞 ${waHref?`<a href="${esc(waHref)}" target=_blank>${esc(a.phone)}</a>`:esc(a.phone)}</span>`:''}
   </div>
 </div>`;
 // --- acciones ---
 h+=`<div class=card><h2>Acciones</h2>
   <div class=actions id=actions>
     <button class="btn ok" id=btnApprove onclick="decide('approve')">✅ Aprobar</button>
     <button class="btn bad" id=btnReject onclick="decide('reject')">❌ Rechazar</button>
     ${conv.chat_url?`<a class="btn ghost" href="${esc(conv.chat_url)}" target=_blank>💬 Abrir chat en Chatwoot</a>`:''}
     ${waHref?`<a class="btn ghost" href="${esc(waHref)}" target=_blank>📱 WhatsApp</a>`:''}
     <span id=dmsg class=sub></span>
   </div>
   <div class=sub style="margin-top:9px">Estado actual: ${appPill}</div>
 </div>`;
 // --- ficha (eval_notes) ---
 h+=`<div class=card><h2>📋 Ficha de calificación</h2>${a.eval_notes?`<div class=ficha>${esc(a.eval_notes)}</div>`:'<div class=mut>sin ficha registrada todavía</div>'}</div>`;
 // --- documentos ---
 const docs=(d.documents||[]);
 const dhtml=docs.length?('<div class=docs>'+docs.map(x=>x.url
    ?`<a href="${esc(x.url)}" target=_blank>${dicon(x.kind)} <span>${esc(x.file_name||x.kind||'archivo')}</span> <span class=k>${esc(x.kind||'')}</span></a>`
    :`<span class=chip>${dicon(x.kind)} ${esc(x.file_name||x.kind||'archivo')} (sin url)</span>`).join('')+'</div>')
   :'<div class=mut>no envió documentos</div>';
 h+=`<div class=card><h2>📎 Documentos (${docs.length})</h2>${dhtml}</div>`;
 // --- conversación (transcripción) ---
 const ms=(d.messages||[]);
 const chat=ms.length?('<div class=chat>'+ms.map(m=>{
    const out=m.direction==='outgoing';
    return `<div class="bub ${out?'bout':'bin'}">${esc(m.content||'')}<span class=bts>${out?'bot':'lead'} · ${ts(m.created_at)}</span></div>`;
   }).join('')+'</div>')
   :'<div class=mut>sin mensajes</div>';
 h+=`<div class=card><h2>💬 Conversación (últimos ${ms.length})</h2>${chat}</div>`;
 h+=`<div style="text-align:center;padding:6px 0 20px"><a href="/" class=b>← Volver al panel</a></div>`;
 $('root').innerHTML=h;
 // baja el scroll del chat al último mensaje
 const cc=document.querySelector('.chat');if(cc)cc.scrollTop=cc.scrollHeight;
}
// Aprobar/Rechazar reutilizan el MISMO proxy server-side del panel (token nunca en el navegador).
async function decide(action){
 const btns=document.querySelectorAll('#actions .btn');
 const msg=$('dmsg'); btns.forEach(b=>{if(b.tagName==='BUTTON')b.disabled=true;});
 msg.textContent=' …';
 try{
   const r=await fetch('/api/'+action+'/'+ID,{method:'POST'});
   const d=await r.json();
   if(d&&d.ok){
     msg.innerHTML=action==='approve'
       ?' <span class="pill pok">✅ aprobado — se le ofrecieron horarios</span>'
       :' <span class="pill pbad">❌ rechazado</span>';
   }else{
     msg.innerHTML=' <span class="pill pbad">'+esc((d&&d.error)||'error')+'</span>';
     btns.forEach(b=>{if(b.tagName==='BUTTON')b.disabled=false;});
   }
 }catch(e){
   msg.innerHTML=' <span class="pill pbad">'+esc(e)+'</span>';
   btns.forEach(b=>{if(b.tagName==='BUTTON')b.disabled=false;});
 }
}
load();
</script></body></html>"""
