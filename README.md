# Framework de Trafiker Digital

**Automatiza captación y ventas por WhatsApp con agentes de IA.** Un anuncio
Click-to-WhatsApp (CTWA) manda gente a tu WhatsApp; un bot con IA los atiende, califica y
guarda todo; un dashboard con dominio propio te deja editar prompts, aprobar leads y leer
las conversaciones. El creativo del anuncio lo genera **Higgsfield**.

Pensado para **trafickers y agencias de marketing** que quieren un embudo de WhatsApp que
trabaje solo: menos clics perdidos, más leads calificados, todo en un panel.

> Este repo está pensado para operarse con **Claude Code**: abre una sesión aquí y Claude
> ya sabe cómo funciona, cómo desplegarlo y qué variables necesita. Lee **`CLAUDE.md`**.

## Quick start (la ruta rápida)

El stack trae su **propio Postgres y Redis** (no provisionas base de datos) y `setup.sh`
autogenera los secretos del panel. Tú solo traes tus **keys externas**.

```
1. git clone <repo> && cd trafiker-reclutador
2. ./setup.sh            # genera secretos del panel
3. Edita .env → pega SOLO tus keys externas (Higgsfield, Meta token + act, OpenAI, Gemini, OpenRouter, Chatwoot, tu teléfono)
4. docker compose up -d  # levanta Postgres + Redis + bot + dashboard
5. Abre http://IP-DE-TU-SERVIDOR:8091  (usuario admin / la clave que imprimió setup.sh)
   (dominio propio = opcional, luego con Traefik/stack.yml)
6. Conecta el webhook de tu inbox Chatwoot → http://IP:8000/webhook/chatwoot
```

Paso a paso y qué es cada key: **`QUICKSTART.md`**.

## El stack

- **WhatsApp Cloud API + Chatwoot** — el canal: recibe los mensajes y por ahí responde el bot.
- **Higgsfield** — la fábrica de creativos: genera las imágenes/videos de los anuncios.
- **Módulos de agentes IA** — cada agente es un *avatar* con su propio prompt y su
  frase-gatillo. Un solo webhook atiende todas las campañas.
- **Dashboard con dominio personalizado** — panel con basic-auth para gestionar y aprobar.
- Debajo: **FastAPI**, **Postgres**, **Redis**, **OpenRouter (LLM)** y **Docker**.

## Qué hace

- **Lanza campañas CTWA** (con el creativo de Higgsfield) que caen en tu WhatsApp.
- **Atiende 24/7** con agentes por rol: cada avatar tiene su persona y su criterio.
- **Enruta por frase-gatillo**: el autofill del anuncio decide qué avatar responde. Un solo
  webhook sirve a todas las campañas/números (no hace falta un endpoint por número).
- **Califica** leads y avisa al dueño (`[CALIFICA: ...]`) o hace handoff a un humano
  (`[HUMANO: ...]`).
- **Entiende** audios (Whisper) e imágenes (Gemini) que envía el lead, y recolecta archivos.
- **Agenda** reuniones/entrevistas y manda recordatorios.
- **Dashboard**: editar prompts (con versionado), aprobar/rechazar, ver el embudo y —si
  conectas Meta— el gasto/CPL por avatar.

## Features

- 🟢 Multi-agente (multi-avatar) con prompts editables en caliente desde el panel.
- 🟢 Ruteo por frase-gatillo del autofill CTWA (atribución fiable sin endpoints extra).
- 🟢 Acumulador/debounce en Redis → responde como humano (junta ráfagas de mensajes).
- 🟢 Marcadores `[CALIFICA]` / `[HUMANO]` con aviso al WhatsApp del dueño.
- 🟢 Entendimiento de media: audio→texto, imagen→descripción.
- 🟢 Generación de creativos con Higgsfield (`app/higgsfield.py`).
- 🟢 Dashboard con dominio propio + basic-auth (Traefik en `dashboard/stack.yml`).
- 🟢 Blindaje anti prompt-injection y regla anti-invención de datos en los prompts.

## Requisitos

- Docker + Docker Compose (el stack trae su **propio Postgres y Redis**, no provisionas DB).
- Un **Chatwoot** con un inbox de **WhatsApp Cloud API**.
- Una API key de **OpenRouter** (para que el bot converse con IA).
- (Opcional) `HIGGSFIELD_API_KEY` para generar creativos de anuncio, `META_TOKEN` +
  `META_AD_ACCOUNT_ID` para lanzar/medir campañas, OpenAI (audio), Gemini (imágenes).

## Detalle

```bash
./setup.sh                   # crea .env y autogenera DASH_PASS/WEBHOOK_SECRET
# edita .env → pega tus keys externas (ver QUICKSTART.md)
docker compose up -d         # Postgres + Redis + bot (:8000) + dashboard (:8091)
docker compose exec recruitbot python -m scripts.seed_agents_phase1   # siembra los agentes
curl localhost:8000/health
```

Configura el webhook de Chatwoot a `http://<IP>:8000/webhook/chatwoot?token=<WEBHOOK_SECRET>`.

Ruta rápida en **`QUICKSTART.md`**; despliegue detallado en **`SETUP.md`**; manual de
operación completo en **`CLAUDE.md`**.

## Documentación

- **`QUICKSTART.md`** — la ruta rápida: de `git clone` a bot corriendo en 6 pasos.
- **`CLAUDE.md`** — el cerebro: arquitectura, tabla de env vars por tier, recetas de
  operación, reglas de seguridad y primeros pasos. **Empieza por aquí.**
- **`SETUP.md`** — despliegue end-to-end (DB, Chatwoot, .env, roles, seed, docker, dominio del panel).
- **`MULTITENANT_DESIGN.md`** — diseño de la config multi-tenant (agentes/prompts en Postgres).

## Licencia

Software propietario, licencia comercial. Ver **`LICENSE`**.
