# CLAUDE.md — Cerebro del sistema (léelo completo antes de operar)

Este archivo es tu manual de operación. Si abres este repo por primera vez, aquí tienes
TODO lo necesario para entender, desplegar y operar el sistema sin contexto previo.
Habla siempre en **español peruano (tú, sin voseo)** con el usuario.

---

## 1. ¿Qué es esto?

El **Framework de Trafiker Digital**: automatiza captación y ventas por WhatsApp con
agentes de IA. Está potenciado por **WhatsApp Cloud API + Chatwoot** (canal), **Higgsfield**
(generación de creativos), **módulos de agentes IA** (cada agente = un avatar con su prompt)
y un **dashboard IP-first** (accesible por IP:8091 al toque; dominio propio con HTTPS es
opcional vía Traefik). Un anuncio manda gente a WhatsApp; un bot con IA los atiende,
califica y guarda todo para que un humano decida. **Postgres y Redis vienen dentro del
stack** (no provisionas base de datos externa).

**Flujo de un mensaje:**

```
Anuncio CTWA (Click-to-WhatsApp; Higgsfield genera el creativo, autofill pre-cargado)
   → WhatsApp Cloud API (el número del negocio)
   → Chatwoot (bandeja única)
   → webhook POST /webhook/chatwoot   (app FastAPI, app/main.py)
   → acumulador Redis (debounce ~15s: junta varios mensajes seguidos = más humano)
   → router de rol (detect_role): la frase-gatillo del autofill decide el AVATAR
   → agente IA: system_prompt del rol + contexto → LLM (OpenRouter)
   → [CALIFICA]/[HUMANO] → aviso al dueño + persiste en Postgres
   → responde por la API de Chatwoot (parte la respuesta, simula tipeo)
   → dashboard (dominio propio): editar prompts, aprobar, ver el embudo
```

**Piezas:**
- `app/` — el bot FastAPI (webhook + loops de fondo: recordatorios, recontacto).
- `app/higgsfield.py` — motor de creativos (imágenes/videos) para los anuncios.
- `dashboard/` — panel FastAPI de lectura/acción con basic-auth. Va en el stack (IP:8091); dominio propio opcional vía Traefik (`dashboard/stack.yml`).
- Postgres — el pipeline y la config de agentes. Redis — el acumulador/debounce.

---

## 2. Arquitectura multi-tenant (cómo se enruta y califica)

- Tablas **`tenants`** (marca/cliente) y **`agents`** (un bot atendiendo un canal). Cada
  agente corresponde a un **`Role`** en `app/roles.py`.
- **Ruteo por frase-gatillo:** cada avatar tiene `trigger_phrases`. El autofill del anuncio
  (el texto pre-cargado en la cajita de WhatsApp) contiene esa frase, así `detect_role()`
  sabe a qué avatar mandar el lead. Si ninguna frase calza, **el bot no responde** (no le
  contesta a randoms). El webhook `/webhook/chatwoot` atiende **cualquier inbox** → NO hace
  falta un endpoint nuevo por número/campaña.
- **Marcadores que emite el agente** (van en su propia línea; el lead NO los ve):
  - `[CALIFICA: ...]` → lead bueno: se marca como calificado y se **avisa al reclutador**.
  - `[HUMANO: ...]` → handoff: que una persona real tome la conversación.
- **Dos formas de definir un rol** (ver `app/roles.py`):
  1. **Reclutamiento** (sin `system_prompt`): el prompt se arma solo con `build_system_prompt()`
     a partir de `title + required_docs + rubric + extra_context`. Ejemplo: `ejemplo-reclutador`.
  2. **Ventas/calificación** (con `system_prompt` propio): tú escribes la persona completa y el
     agente emite `[CALIFICA: ...]`. Ejemplo: `ejemplo-vendedor`.
- El **prompt ACTIVO vive en la tabla `agents`** (editable en el panel, con versionado en
  `agent_prompt_versions`). `roles.py` es la fuente para el **seed** inicial y el fallback en disco.
  Editar en el panel aplica al instante (sin redeploy); editar `roles.py` requiere re-seed.

---

## 3. Variables de entorno (dos bloques)

Corre `./setup.sh` (crea `.env` y autogenera los secretos del panel) y luego llena SOLO
las keys externas. `.env` está en `.gitignore` (**nunca lo commitees**). El modelo mental:
**tú traes las keys externas; la infra (DB, caché, secretos del panel) se la provee/genera
el stack.**

### 🔑 KEYS EXTERNAS (el CORE — las traes tú, de sus paneles)
| Var | Qué es | ¿Secreto? |
|---|---|---|
| `HIGGSFIELD_API_KEY` | creativos (imágenes/videos) para los anuncios | 🔒 sí |
| `META_TOKEN` | ads Graph API (System User de Meta Business); gasto/CPL + leads | 🔒 sí |
| `META_AD_ACCOUNT_ID` | `act_XXXXXXXXX` — la cuenta publicitaria para lanzar campañas | no |
| `OPENAI_API_KEY` | transcribir audios de WhatsApp (Whisper) | 🔒 sí |
| `GEMINI_API_KEY` | entender imágenes que envía el lead | 🔒 sí |
| `OPENROUTER_API_KEY` | cerebro del agente / LLM (OpenAI-compatible). Vacío = templated | 🔒 sí |
| `CHATWOOT_URL` | URL de tu Chatwoot | no |
| `CHATWOOT_ACCOUNT_ID` | ID de la cuenta de Chatwoot | no |
| `CHATWOOT_TOKEN` | API access token de Chatwoot | 🔒 sí |
| `CHATWOOT_INBOX_ID` | inbox del número WhatsApp | no |
| `RECRUITER_PHONE` | WhatsApp que recibe avisos de leads/CVs | dato personal |

### ⚙️ INFRA (interna / autogenerada — normalmente NO la tocas)
| Var | Qué es | ¿Secreto? |
|---|---|---|
| `DATABASE_URL` | Postgres **INTERNO** del stack (`db:5432`); no provisionas DB externa | (interno) |
| `REDIS_URL` | Redis **INTERNO** del stack (`redis:6379`); acumulador/debounce | (interno) |
| `DASH_USER` | usuario del dashboard (basic-auth); default `admin` | no |
| `DASH_PASS` | clave del dashboard. **Vacío → `setup.sh` lo autogenera** | 🔒 sí |
| `WEBHOOK_SECRET` | protege el webhook (`?token=`) + aprobar desde el panel. **Vacío → `setup.sh` lo autogenera** | 🔒 sí |

- **Postgres + Redis son servicios internos del stack** (Swarm/compose), sin puerto
  publicado al host: no hay DB externa que provisionar y no chocan con nada del servidor.
  Sus URLs por defecto ya apuntan a `db`/`redis` y el compose las fuerza a los internos.
- **`DASH_PASS` y `WEBHOOK_SECRET` los autogenera `setup.sh`** (`openssl rand -hex 12`) si
  quedan vacíos; es idempotente (no pisa lo ya puesto).

### 🟢 OPCIONALES (defaults ok)
| Var | Para qué | ¿Secreto? |
|---|---|---|
| `HIGGSFIELD_BASE_URL` | base URL de la API de Higgsfield (default razonable) | no |
| `EVOLUTION_URL` / `EVOLUTION_KEY` / `EVOLUTION_INSTANCE` | recontacto por WhatsApp personal fuera de 24h | 🔒 la KEY |
| `LLM_MODEL` | id del modelo (default `openai/gpt-4.1-mini`) | no |
| `DEBOUNCE_SECONDS` | segundos que acumula antes de responder | no |
| `DASH_EXTRA_USERS` | usuarios extra del panel `u1:p1,u2:p2` | 🔒 sí |
| `BOT_INTERNAL_URL` | URL bot↔dashboard para aprobar desde el panel | no |
| `EVALUATION_DAYS`, `INTERVIEW_SLOTS`, `INTERVIEW_SEDE_ENABLED`, `REMINDER_HOUR` | timing de reuniones/recordatorios | no |

---

## 4. Cómo operar por chat (recetas)

**Crear un agente/avatar nuevo:**
1. Edita `app/roles.py`: agrega un `Role` al dict `ROLES`. Define `trigger_phrases` (deben
   coincidir con el autofill del anuncio), `required_docs`, `rubric`, e `intro`. Para ventas,
   escribe un `system_prompt` que emita `[CALIFICA: ...]`.
2. Siembra en la DB: `python -m scripts.seed_agents_phase1`
3. Reconstruye el bot: `docker compose build && docker compose up -d`
4. (Opcional) desde el panel afina el prompt (queda versionado).

**Prender / apagar un agente:**
- Desde el dashboard: toggle `active`.
- Por SQL: `UPDATE agents SET active = false WHERE slug = 'ejemplo-vendedor';`

**Editar el prompt de un agente:**
- Desde el dashboard (recomendado): se guarda una nueva **versión** y aplica al instante.
- O edita `roles.py` y vuelve a correr el seed (crea una versión nueva si cambió).

**Lanzar una campaña (CTWA):**
- Patrón: campaña de Click-to-WhatsApp cuyo **autofill** contiene la `trigger_phrase` del avatar.
- **SIEMPRE crea la campaña en PAUSA** y **confirma con el usuario antes de activar** (antes de
  gastar dinero). Ver reglas abajo.

---

## 5. Reglas de seguridad (obligatorias)

- **Campañas SIEMPRE en PAUSA + confirmar antes de gastar.** Nunca actives pauta sin OK explícito.
- **Nunca inventes datos.** Direcciones, montos, links, fechas, nombres: si no los tienes, se
  confirman por el mismo medio. Esta regla ya está incrustada en los prompts.
- **Nunca commitees secretos.** El `.env` no va a git. No pegues tokens en el código.
- **Español peruano (tú).** Nada de voseo (-á/-é/-í).
- **A candidatos:** contáctalos solo por el número oficial; fuera de la ventana de 24h usa
  plantillas aprobadas (o el recontacto vía Evolution si está configurado).

---

## 6. Comandos

```bash
./setup.sh                       # 1) crea .env y autogenera DASH_PASS/WEBHOOK_SECRET
# 2) edita .env → pega tus keys externas
# levantar TODO (Postgres + Redis + bot + dashboard):
docker compose up -d --build     # bot en :8000, dashboard en :8091 (Postgres/Redis internos)
docker compose build             # reconstruir tras cambiar código/roles
docker compose logs -f recruitbot

# seeds (corren dentro del contenedor):
docker compose exec recruitbot python -m scripts.seed_agents_phase1   # tablas tenants/agents + siembra roles.py
docker compose exec recruitbot python -m scripts.seed_tools           # tools_catalog (tools activables por agente)

# salud:
curl localhost:8000/health
```

**Dashboard (IP-first, incluido en el stack):** ya sube con `docker compose up -d` y queda
en `http://IP-DEL-SERVIDOR:8091` (basic-auth `DASH_USER`/`DASH_PASS`). No hay que desplegar
nada aparte para empezar.

**Dominio propio (OPCIONAL, cuando quieras HTTPS con dominio):** usa `dashboard/stack.yml`
(Swarm + Traefik). Cambia la regla `traefik.http.routers.botpanel.rule: "Host(`panel.example.com`)"`
por tu dominio real y apunta ese DNS al servidor; el `certresolver: le` emite el TLS. Sin
Swarm/Traefik, enruta el dominio al puerto 8000 del contenedor con tu reverse-proxy.

---

## 7. PRIMEROS PASOS (sigue esto al abrir el repo por primera vez)

1. **Prepara el `.env`:** `./setup.sh` (crea `.env` y autogenera `DASH_PASS`/`WEBHOOK_SECRET`).
   NO necesitas crear ninguna base de datos: Postgres y Redis los levanta el stack.
2. **Chatwoot:** crea/identifica la cuenta y el **inbox** de WhatsApp; anota `CHATWOOT_ACCOUNT_ID`
   e `CHATWOOT_INBOX_ID` y genera un `CHATWOOT_TOKEN`.
3. **Llena `.env`:** pega SOLO tus **keys externas** (Higgsfield, Meta token + `META_AD_ACCOUNT_ID`,
   OpenAI, Gemini, OpenRouter, Chatwoot, tu teléfono).
4. **Define tus roles:** edita `app/roles.py` (reemplaza los dos ejemplos por tus avatares reales;
   cada uno con su frase-gatillo = el autofill del anuncio).
5. **Levanta TODO:** `docker compose up -d --build` (Postgres + Redis + bot + dashboard) y
   verifica `curl localhost:8000/health`.
6. **Siembra:** `docker compose exec recruitbot python -m scripts.seed_agents_phase1` y
   `docker compose exec recruitbot python -m scripts.seed_tools`.
7. **Abre el panel:** `http://IP-DEL-SERVIDOR:8091` (admin / la clave que imprimió `setup.sh`).
   Dominio propio con HTTPS = opcional, luego con `dashboard/stack.yml`.
8. **Webhook:** en Chatwoot → Integrations → Webhooks, apunta a
   `http://<IP>:8000/webhook/chatwoot?token=<WEBHOOK_SECRET>`, evento *Message created*.
9. **Primera campaña en PAUSA:** arma el anuncio CTWA con el autofill de un avatar, déjalo en
   pausa y **confirma con el usuario antes de activarlo**.

Detalle paso a paso en **SETUP.md**. Visión de producto en **README.md**.
