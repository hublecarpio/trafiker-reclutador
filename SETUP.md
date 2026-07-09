# SETUP — Despliegue end-to-end

Guía paso a paso para poner el sistema en marcha. Referencia rápida de operación: `CLAUDE.md`.

---

## 0. Requisitos previos

- Docker + Docker Compose en el servidor.
- Un **Postgres** accesible (puede ser el que trae `docker-compose` si agregas uno, o uno externo).
- Un **Chatwoot** con un **inbox de WhatsApp** (API oficial de WhatsApp Cloud recomendado).
- Una **API key de OpenRouter** (LLM). Opcional: OpenAI (audio), Gemini (imágenes), token de Meta.

---

## 1. Base de datos (Postgres)

1. Crea una base vacía, por ejemplo `recruitment`.
2. Arma el `DATABASE_URL` con driver asyncpg:
   `postgresql+asyncpg://usuario:password@host:5432/recruitment`
3. Las tablas del pipeline las crea el bot al arrancar (SQLAlchemy). Las tablas multi-tenant
   (`tenants`, `agents`, `agent_prompt_versions`) las crea el seed del paso 6.

---

## 2. Chatwoot (bandeja + webhook)

1. Identifica tu **Account ID** (`CHATWOOT_ACCOUNT_ID`) y crea/ubica el **inbox** de WhatsApp
   (`CHATWOOT_INBOX_ID`).
2. Genera un **access token** (`CHATWOOT_TOKEN`): perfil → *Access Token* (o un agente-bot).
3. Configura el **webhook**: Settings → Integrations → **Webhooks** → Add:
   - URL: `https://<tu-host>/webhook/chatwoot?token=<WEBHOOK_SECRET>`
   - Evento: **Message created**
   - (En pruebas puedes exponer el bot con un túnel, ej. Cloudflare quick tunnel, y usar esa URL.)

---

## 3. Variables de entorno

```bash
cp .env.template .env
```
Llena al menos las 🔴 **obligatorias** (`DATABASE_URL`, `REDIS_URL`, `CHATWOOT_*`,
`OPENROUTER_API_KEY`, `RECRUITER_PHONE`, `DASH_USER`, `DASH_PASS`). Activa las 🟡/🟢 según uses
Meta, webhook seguro, audio, imágenes o recontacto. La clasificación completa está en `CLAUDE.md`.

> `.env` está en `.gitignore`. **Nunca lo subas a git.**

---

## 4. Define tus roles (avatares)

Edita `app/roles.py`. Vienen **dos ejemplos** (`ejemplo-vendedor` con `system_prompt` propio y
`ejemplo-reclutador` que usa la plantilla). Reemplázalos por tus roles reales:

- `trigger_phrases`: deben coincidir con el **autofill** del anuncio de ese avatar.
- `required_docs`, `rubric`, `intro`: qué pide y a quién busca.
- Para ventas/calificación, escribe un `system_prompt` que emita `[CALIFICA: ...]`.

---

## 5. Levanta el bot

```bash
docker compose up -d --build     # bot en :8090, redis en :16380
docker compose logs -f recruitbot
curl localhost:8090/health
```

---

## 6. Siembra la config en la DB

```bash
python -m scripts.seed_agents_phase1   # tablas tenants/agents + una fila por rol de roles.py
python -m scripts.seed_tools           # tools_catalog (tools activables por agente)
```
(Corre estos con las dependencias instaladas: dentro del contenedor del bot, o en un venv con
`pip install -r requirements.txt` y el `.env` cargado.)

Vuelve a correr el seed cada vez que cambies `roles.py` (crea una versión nueva si el prompt cambió).

---

## 7. Despliega el dashboard

Servicio aparte, con su propio dominio y **basic-auth**.

```bash
docker build -t recruitagent/dashboard:latest ./dashboard
```

- Pásale las mismas variables (`DATABASE_URL`, `CHATWOOT_*`, `META_TOKEN`, `DASH_USER`,
  `DASH_PASS`, `WEBHOOK_SECRET`, `BOT_INTERNAL_URL`, etc.).
- Ponlo detrás de HTTPS. Para aprobar candidatos desde el panel, define `BOT_INTERNAL_URL`
  (URL interna hacia el bot) y `WEBHOOK_SECRET` (el token viaja server-side, nunca al navegador).
- `dashboard/stack.yml` trae un ejemplo de despliegue como stack de Swarm ruteado por Traefik;
  adáptalo a tu infra o usa un simple `docker run` con las envs.

---

## 8. Primera campaña (en PAUSA)

1. Arma el anuncio **CTWA** con el **autofill** que contenga la `trigger_phrase` del avatar.
2. Déjalo **en PAUSA**.
3. **Confirma con el usuario antes de activar** (antes de gastar dinero). Regla dura.
4. Cuando entren mensajes, revísalos en el dashboard: embudo, conversaciones y candidatos por aprobar.

---

## Verificación rápida

- `curl localhost:8090/health` responde OK.
- Un mensaje de prueba con la frase-gatillo de un rol genera respuesta del bot y aparece en Postgres
  (`applicants`, `conversations`, `messages`).
- El dashboard lista los agentes sembrados y deja editar un prompt.
