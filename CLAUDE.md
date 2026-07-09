# CLAUDE.md — Cerebro del sistema (léelo completo antes de operar)

Este archivo es tu manual de operación. Si abres este repo por primera vez, aquí tienes
TODO lo necesario para entender, desplegar y operar el sistema sin contexto previo.
Habla siempre en **español peruano (tú, sin voseo)** con el usuario.

---

## 1. ¿Qué es esto?

El **Framework de Trafiker Digital**: automatiza captación y ventas por WhatsApp con
agentes de IA. Está potenciado por **WhatsApp Cloud API + Chatwoot** (canal), **Higgsfield**
(generación de creativos), **módulos de agentes IA** (cada agente = un avatar con su prompt)
y un **dashboard con dominio personalizado**. Un anuncio manda gente a WhatsApp; un bot con
IA los atiende, califica y guarda todo para que un humano decida.

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
- `dashboard/` — panel FastAPI de lectura/acción con su propio dominio + basic-auth. Servicio aparte.
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

## 3. Variables de entorno por tier

Copia `.env.template` → `.env` y llena. `.env` está en `.gitignore` (**nunca lo commitees**).

### 🔴 OBLIGATORIAS (sin ellas no arranca / no responde)
| Var | Qué es | ¿Secreto? |
|---|---|---|
| `DATABASE_URL` | Postgres (pipeline + agentes), driver asyncpg | 🔒 sí (lleva password) |
| `REDIS_URL` | Redis del acumulador/debounce | no |
| `CHATWOOT_URL` | URL de tu Chatwoot | no |
| `CHATWOOT_ACCOUNT_ID` | ID de la cuenta de Chatwoot | no |
| `CHATWOOT_TOKEN` | API access token de Chatwoot | 🔒 sí |
| `OPENROUTER_API_KEY` | LLM (OpenAI-compatible). Vacío = respuestas templated | 🔒 sí |
| `RECRUITER_PHONE` | WhatsApp que recibe avisos de leads/CVs | dato personal |
| `DASH_USER` | usuario del dashboard (basic-auth) | no |
| `DASH_PASS` | clave del dashboard. Vacío = panel sin login (solo local) | 🔒 sí |

### 🟡 SEGÚN USO
| Var | Para qué | ¿Secreto? |
|---|---|---|
| `META_TOKEN` | leer leads del formulario de Meta y ver gasto/CPL en el panel | 🔒 sí |
| `WEBHOOK_SECRET` | proteger el webhook (`?token=`) + aprobar desde el dashboard | 🔒 sí |
| `CHATWOOT_INBOX_ID` | inbox desde el que se notifican/agendan las citas | no |

### 🟢 OPCIONALES
| Var | Para qué | ¿Secreto? |
|---|---|---|
| `HIGGSFIELD_API_KEY` | generación de creativos (imágenes/videos) para los anuncios | 🔒 sí |
| `HIGGSFIELD_BASE_URL` | base URL de la API de Higgsfield (default razonable) | no |
| `OPENAI_API_KEY` | transcribir audios de WhatsApp (Whisper) | 🔒 sí |
| `GEMINI_API_KEY` | entender imágenes que envía el lead | 🔒 sí |
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
cp .env.template .env            # 1) copia y llena las variables
# levantar todo (bot + redis):
docker compose up -d --build     # bot en :8090, redis en :16380
docker compose build             # reconstruir tras cambiar código/roles
docker compose logs -f recruitbot

# seeds (corren dentro del contenedor o en un venv con las deps):
python -m scripts.seed_agents_phase1   # crea tablas tenants/agents + siembra roles.py
python -m scripts.seed_tools           # crea tools_catalog (tools activables por agente)

# salud:
curl localhost:8090/health
```

**Dashboard (servicio aparte, con DOMINIO PERSONALIZADO + basic-auth):**
```bash
docker build -t trafiker/dashboard:latest ./dashboard
# despliega con su stack (ver dashboard/stack.yml) o docker run pasándole las mismas envs;
# protégelo con DASH_USER/DASH_PASS y ponlo detrás de HTTPS.
```
- **Dominio propio:** en `dashboard/stack.yml` cambia la regla de Traefik
  `traefik.http.routers.botpanel.rule: "Host(`panel.example.com`)"` por tu dominio real
  (ej. `panel.tuagencia.com`) y apunta ese DNS al servidor. El `certresolver: le` emite
  el TLS automático. Sin Swarm/Traefik, usa tu reverse-proxy y enruta ese dominio al puerto 8000.

---

## 7. PRIMEROS PASOS (sigue esto al abrir el repo por primera vez)

1. **Crea la base de datos** Postgres (vacía) y arma su `DATABASE_URL`.
2. **Chatwoot:** crea/identifica la cuenta y el **inbox** de WhatsApp; anota `CHATWOOT_ACCOUNT_ID`
   e `CHATWOOT_INBOX_ID` y genera un `CHATWOOT_TOKEN`.
3. **Webhook:** en Chatwoot → Integrations → Webhooks, apunta a
   `https://<tu-host>/webhook/chatwoot?token=<WEBHOOK_SECRET>`, evento *Message created*.
4. **Llena `.env`:** `cp .env.template .env` y completa al menos las 🔴 obligatorias.
5. **Define tus roles:** edita `app/roles.py` (reemplaza los dos ejemplos por tus avatares reales;
   cada uno con su frase-gatillo = el autofill del anuncio).
6. **Siembra:** `python -m scripts.seed_agents_phase1` y `python -m scripts.seed_tools`.
7. **Levanta el bot:** `docker compose up -d --build` y verifica `curl localhost:8090/health`.
8. **Despliega el panel** con su dominio y basic-auth (`DASH_USER`/`DASH_PASS`).
9. **Primera campaña en PAUSA:** arma el anuncio CTWA con el autofill de un avatar, déjalo en
   pausa y **confirma con el usuario antes de activarlo**.

Detalle paso a paso en **SETUP.md**. Visión de producto en **README.md**.
