# Recruit Agent Framework

Motor de **CRM conversacional multi-agente** para **reclutamiento y ventas por WhatsApp**.
Un anuncio Click-to-WhatsApp (CTWA) manda gente a tu WhatsApp; un bot con IA los atiende,
califica y guarda todo; un dashboard te deja editar prompts, aprobar candidatos y leer chats.

> Este repo está pensado para operarse con **Claude Code**: abre una sesión aquí y Claude ya
> sabe cómo funciona, cómo desplegarlo y qué variables necesita. Lee **`CLAUDE.md`**.

## Qué hace

- Atiende WhatsApp 24/7 con **agentes por rol** (avatares), cada uno con su propia persona.
- **Enruta por frase-gatillo**: el autofill del anuncio decide qué avatar responde. Un solo
  webhook sirve a todas las campañas/números (no hace falta un endpoint por número).
- **Califica** leads/candidatos y avisa al reclutador (`[CALIFICA: ...]`) o hace handoff a un
  humano (`[HUMANO: ...]`).
- **Recolecta** CV, portafolios, audios (Whisper) e imágenes (Gemini) y los deja en el pipeline.
- **Agenda entrevistas** y manda recordatorios; **recontacta** fuera de las 24h (opcional).
- **Dashboard**: editar prompts (con versionado), aprobar/rechazar candidatos, ver el embudo y,
  si conectas Meta, el gasto/CPL por avatar.

## Stack

- **FastAPI** (bot `app/` + dashboard `dashboard/`)
- **Postgres** (pipeline + config de agentes) · **Redis** (acumulador/debounce)
- **Chatwoot** (bandeja WhatsApp) · **OpenRouter** (LLM, OpenAI-compatible)
- Opcionales: **OpenAI** (audio), **Gemini** (imágenes), **Meta Graph** (leads/insights),
  **Evolution API** (recontacto WhatsApp personal)
- **Docker Compose** para el bot; el dashboard se despliega como servicio aparte

## Requisitos

- Docker + Docker Compose
- Un Postgres accesible y un Chatwoot con un inbox de WhatsApp
- Una API key de OpenRouter (para que el bot converse con IA)

## Quickstart

```bash
cp .env.template .env        # llena al menos las variables 🔴 obligatorias
# edita app/roles.py con tus avatares (los dos que vienen son ejemplos)
docker compose up -d --build # bot en :8090
python -m scripts.seed_agents_phase1   # siembra los roles en la DB
curl localhost:8090/health
```

Configura el webhook de Chatwoot a `https://<tu-host>/webhook/chatwoot?token=<WEBHOOK_SECRET>`.

## Documentación

- **`CLAUDE.md`** — el cerebro: arquitectura, tabla de env vars por tier, recetas de operación,
  reglas de seguridad y primeros pasos. **Empieza por aquí.**
- **`SETUP.md`** — despliegue end-to-end paso a paso (DB, Chatwoot, .env, roles, seed, docker, panel, campaña).
- **`MULTITENANT_DESIGN.md`** — diseño de la config multi-tenant (agentes/prompts en Postgres).

## Licencia

Software propietario, licencia comercial. Ver **`LICENSE`**.
