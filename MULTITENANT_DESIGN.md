# Multi-tenant: prompts y config en Postgres (no hardcodeado)

**Meta:** dejar de duplicar un workflow por cliente (hoy 100+ en n8n) y de hardcodear
roles en `roles.py`. Pasar a **1 acumulador + 1 cerebro genéricos** que leen la config
(incluido el system_prompt) de una tabla Postgres, resuelta por el canal que entra.

Regla de oro: **un mensaje entra → se resuelve el `agent` por su clave de ruteo →
se carga su fila → el cerebro usa `system_prompt`, `model`, `tools` de esa fila.**

---

## 1. Esquema Postgres (fuente única de verdad)

```sql
-- CLIENTE / marca (un tenant puede tener varios agentes)
CREATE TABLE tenants (
  id          BIGSERIAL PRIMARY KEY,
  slug        TEXT UNIQUE NOT NULL,         -- 'marca-a', 'marca-b', 'default'
  name        TEXT NOT NULL,
  active      BOOLEAN NOT NULL DEFAULT true,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- AGENTE = un bot atendiendo un canal/inbox concreto
CREATE TABLE agents (
  id            BIGSERIAL PRIMARY KEY,
  tenant_id     BIGINT REFERENCES tenants(id),
  slug          TEXT UNIQUE NOT NULL,       -- 'ejemplo-vendedor', 'ejemplo-reclutador'
  name          TEXT NOT NULL,
  active        BOOLEAN NOT NULL DEFAULT true,

  -- ── RUTEO (cómo un mensaje encuentra a este agente) ──
  channel               TEXT NOT NULL,      -- whatsapp_cloud | evolution | chatwoot | instagram
  chatwoot_account_id   INT,
  chatwoot_inbox_id     INT,                -- CLAVE PRIMARIA de ruteo (todo cae en un inbox)
  waba_phone_number_id  TEXT,               -- para CTWA / Cloud API
  meta_page_id          TEXT,
  evolution_instance    TEXT,
  autofill_phrase       TEXT,               -- "Hola, quiero vender mi camioneta" (attribution CTWA)

  -- ── CEREBRO (lo que el user quiere variable) ──
  system_prompt   TEXT NOT NULL,            -- ⭐ el prompt, editable, NO hardcodeado
  model           TEXT NOT NULL DEFAULT 'anthropic/claude-haiku-4.5',
  temperature     NUMERIC DEFAULT 0.4,
  debounce_seconds INT DEFAULT 15,
  use_memory      BOOLEAN DEFAULT true,     -- memoria Postgres del chat
  kb_collection   TEXT,                     -- colección vectorial (RAG) si aplica

  -- ── CAPACIDADES / HANDOFF ──
  tools           JSONB DEFAULT '[]',       -- ["agendar_cita","enviar_fotos","notificar_reclutador"]
  notify_phone    TEXT,                     -- a quién avisa (WhatsApp del dueño)
  settings        JSONB DEFAULT '{}',       -- extras sin migrar schema (precios, sede, etc.)

  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_agents_inbox  ON agents(chatwoot_inbox_id) WHERE active;
CREATE INDEX idx_agents_waba   ON agents(waba_phone_number_id) WHERE active;
CREATE INDEX idx_agents_evo    ON agents(evolution_instance) WHERE active;

-- HISTORIAL de prompts (editar sin miedo + rollback)
CREATE TABLE agent_prompt_versions (
  id          BIGSERIAL PRIMARY KEY,
  agent_id    BIGINT REFERENCES agents(id),
  version     INT NOT NULL,
  system_prompt TEXT NOT NULL,
  note        TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

## 2. Resolución de tenant (ruteo)

El **`chatwoot_inbox_id`** es la clave universal: todo canal (WhatsApp oficial, Evolution,
IG) aterriza en un inbox de Chatwoot. Un inbox = un agente.

```
mensaje entra → tomar inbox_id (o waba_phone_number_id / evolution_instance)
             → SELECT * FROM agents WHERE chatwoot_inbox_id = :inbox AND active LIMIT 1
             → si CTWA con varios roles en el mismo número: desempatar por autofill_phrase
```

Esto ya lo haces a medias: el bot Python tiene `INBOX_ROLE_MAP="+51999999999:analytics-engineer"`.
Lo formalizamos en tabla.

## 3. Cómo cambian los workflows n8n (de 100 a 2)

**Acumulador genérico** (igual al tuyo): tras juntar el batch de Redis, agrega **1 nodo
Postgres "Get Agent"** = `SELECT ... WHERE chatwoot_inbox_id = {{ inbox }}`. Pasa
`system_prompt`, `model`, `tools` aguas abajo.

**Cerebro genérico** (igual al `FORTHEM_BOT...`): en el **AI Agent node**:
- System message = `{{ $('Get Agent').item.json.system_prompt }}`  ← ya no hardcodeado
- Modelo = elegido por `{{ ...model }}` (un Switch corto OpenRouter/Gemini/OpenAI)
- Memoria Postgres key = `agent_id + conversation_id`
- Tools: déjalos todos cableados y **gateados por el campo `tools`** (el prompt indica
  cuáles usar). *(n8n no agrega nodos-tool dinámicos; por eso se cablean todos y se filtran.)*

Resultado: **1 acumulador + 1 cerebro** sirven a TODOS los clientes. Cliente nuevo =
fila nueva en `agents`, cero workflows nuevos.

## 4. El bot Python converge a la MISMA tabla

`roles.py` (dict hardcodeado) → cargar de `agents` (cache 60s, refresca solo).
`build_system_prompt()` usa `agent.system_prompt`; `detect_role()` resuelve por inbox/autofill.
Así n8n y el bot Python leen **la misma** config → una sola verdad.

## 5. Migración SIN romper lo que ya corre

- **Fase 0** — crear las tablas (aditivo, nada se rompe).
- **Fase 1** — backfill: 1 fila en `agents` por cada workflow/rol actual, copiando su
  prompt hardcodeado a `system_prompt` (script extractor desde n8n API + roles.py).
- **Fase 2** — construir el acumulador+cerebro genéricos y probarlos con **1 tenant de bajo
  riesgo** (apuntar el webhook de ese inbox al pipeline genérico). El resto sigue igual.
- **Fase 3** — migrar tenants de uno en uno (cambiar el webhook del inbox al genérico),
  dejando el workflow viejo como fallback hasta validar.
- **Fase 4** — apagar los workflows duplicados.
- **Fase 5** — en el panel botmulticamp: ABM de agentes + editor de `system_prompt` con
  versiones (`agent_prompt_versions`) y toggle activo. Como el prompt se lee por mensaje
  (o cache corto), **editas en el panel y aplica al instante, sin redeploy**.

## 6. Lo que ganas
- 1 cambio de prompt en el panel ≠ editar 1 de 100 workflows.
- Cliente nuevo en 1 minuto (una fila).
- Observabilidad por tenant (el panel ya está).
- n8n y Python comparten cerebro/config → fin de la duplicación.
