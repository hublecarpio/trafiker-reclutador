"""Fase 0+1a: crea las tablas multi-tenant y siembra los prompts del bot Python
(roles.py) como filas en `agents`. ADITIVO: no toca tablas existentes ni cambia el bot
(el bot aún no lee estas tablas). Idempotente (re-ejecutable)."""
import asyncio, os, json
import asyncpg
from app.roles import ROLES, build_system_prompt
from app.config import settings

DDL = """
CREATE TABLE IF NOT EXISTS tenants (
  id BIGSERIAL PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agents (
  id BIGSERIAL PRIMARY KEY,
  tenant_id BIGINT REFERENCES tenants(id),
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true,
  channel TEXT NOT NULL DEFAULT 'chatwoot',
  chatwoot_account_id INT,
  chatwoot_inbox_id INT,
  waba_phone_number_id TEXT,
  meta_page_id TEXT,
  evolution_instance TEXT,
  autofill_phrase TEXT,
  system_prompt TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT 'anthropic/claude-haiku-4.5',
  temperature NUMERIC DEFAULT 0.4,
  debounce_seconds INT DEFAULT 15,
  use_memory BOOLEAN DEFAULT true,
  kb_collection TEXT,
  tools JSONB DEFAULT '[]'::jsonb,
  notify_phone TEXT,
  settings JSONB DEFAULT '{}'::jsonb,
  source TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_agents_inbox ON agents(chatwoot_inbox_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_agents_waba  ON agents(waba_phone_number_id) WHERE active;
CREATE INDEX IF NOT EXISTS idx_agents_evo   ON agents(evolution_instance) WHERE active;
CREATE TABLE IF NOT EXISTS agent_prompt_versions (
  id BIGSERIAL PRIMARY KEY,
  agent_id BIGINT REFERENCES agents(id),
  version INT NOT NULL,
  system_prompt TEXT NOT NULL,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

async def main():
    url = settings.database_url.replace("+asyncpg", "")
    c = await asyncpg.connect(url)
    # Fase 0
    await c.execute(DDL)
    print("Fase 0: tablas creadas/verificadas (tenants, agents, agent_prompt_versions)")

    # tenant por defecto (todos los roles viven aquí por ahora). Renómbralo a tu marca.
    tid = await c.fetchval("""
        INSERT INTO tenants(slug,name) VALUES('default','Mi Empresa')
        ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name RETURNING id
    """)

    # inbox de Chatwoot por defecto para los agentes nuevos (viene de .env)
    inbox_id = int(settings.chatwoot_inbox_id) if str(settings.chatwoot_inbox_id).isdigit() else None

    # Fase 1a: una fila por rol del bot Python
    n_new = n_upd = 0
    for slug, role in ROLES.items():
        sp = build_system_prompt(role)
        settings_json = json.dumps({
            "title": role.title,
            "required_docs": getattr(role, "required_docs", None),
            "rubric": getattr(role, "rubric", None),
            "no_fixed_timeline": getattr(role, "no_fixed_timeline", False),
        }, ensure_ascii=False)
        row = await c.fetchrow("SELECT id, system_prompt FROM agents WHERE slug=$1", slug)
        if row is None:
            aid = await c.fetchval("""
                INSERT INTO agents(tenant_id,slug,name,channel,chatwoot_inbox_id,
                                   system_prompt,model,settings,source)
                VALUES($1,$2,$3,'chatwoot',$7,$4,$5,$6::jsonb,'python:roles.py')
                RETURNING id
            """, tid, slug, role.title, sp, settings.llm_model, settings_json, inbox_id)
            await c.execute("""INSERT INTO agent_prompt_versions(agent_id,version,system_prompt,note)
                               VALUES($1,1,$2,'seed inicial desde roles.py')""", aid, sp)
            n_new += 1
        else:
            # actualizar prompt si cambió + nueva versión
            if row["system_prompt"] != sp:
                await c.execute("UPDATE agents SET system_prompt=$2, updated_at=now() WHERE id=$1", row["id"], sp)
                v = await c.fetchval("SELECT COALESCE(MAX(version),0)+1 FROM agent_prompt_versions WHERE agent_id=$1", row["id"])
                await c.execute("""INSERT INTO agent_prompt_versions(agent_id,version,system_prompt,note)
                                   VALUES($1,$2,$3,'re-seed roles.py')""", row["id"], v, sp)
                n_upd += 1

    tot = await c.fetchval("SELECT count(*) FROM agents")
    print(f"Fase 1a: roles sembrados → nuevos={n_new} actualizados={n_upd} | total agents={tot}")
    print("\nAGENTES EN LA TABLA:")
    for r in await c.fetch("SELECT slug,name,model,length(system_prompt) AS plen FROM agents ORDER BY slug"):
        print(f"  {r['slug']:28} | {r['model']:26} | prompt {r['plen']} chars | {r['name']}")
    await c.close()

asyncio.run(main())
