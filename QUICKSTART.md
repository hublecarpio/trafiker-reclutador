# QUICKSTART — la ruta rápida

De cero a bot corriendo en 6 pasos. El stack trae su propio **Postgres** y **Redis**
(no provisionas base de datos) y `setup.sh` autogenera los secretos del panel. Tú solo
traes tus **keys externas**.

```
1. git clone <repo> && cd trafiker-reclutador
2. ./setup.sh            # genera secretos del panel
3. Edita .env → pega SOLO tus keys externas (Higgsfield, Meta token + act, OpenAI, Gemini, OpenRouter, Chatwoot, tu teléfono)
4. docker compose up -d  # levanta Postgres + Redis + bot + dashboard
5. Abre http://IP-DE-TU-SERVIDOR:8091  (usuario admin / la clave que imprimió setup.sh)
   (dominio propio = opcional, luego con Traefik/stack.yml)
6. Conecta el webhook de tu inbox Chatwoot → http://IP:8000/webhook/chatwoot
```

## Qué es cada cosa

- **`./setup.sh`** — copia `.env.template` → `.env` (si no existe) y autogenera
  `DASH_PASS` y `WEBHOOK_SECRET`. Es idempotente: re-córrelo sin miedo, nunca pisa
  valores que ya pusiste.
- **Keys externas (el CORE, las traes tú):** `HIGGSFIELD_API_KEY`, `META_TOKEN`,
  `META_AD_ACCOUNT_ID` (`act_XXXX`), `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `OPENROUTER_API_KEY`, `CHATWOOT_URL` / `CHATWOOT_ACCOUNT_ID` / `CHATWOOT_TOKEN` /
  `CHATWOOT_INBOX_ID`, `RECRUITER_PHONE`.
- **Infra (interna, no la tocas):** `DATABASE_URL` y `REDIS_URL` ya apuntan a los
  servicios internos del stack (`db` y `redis`). Sin puerto al host: no chocan con
  nada del servidor.
- **Dashboard IP-first:** queda en `http://IP-DE-TU-SERVIDOR:8091` de una. Si más
  adelante quieres un **dominio propio con HTTPS**, usa `dashboard/stack.yml`
  (Swarm + Traefik) — es opcional.

## Siembra los agentes (una vez levantado)

```bash
docker compose exec recruitbot python -m scripts.seed_agents_phase1
docker compose exec recruitbot python -m scripts.seed_tools
```

Detalle completo en **`SETUP.md`**; manual de operación en **`CLAUDE.md`**.
