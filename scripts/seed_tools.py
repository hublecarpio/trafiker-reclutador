"""Crea la tabla tools_catalog (para que el dashboard liste los tools disponibles)
y la llena desde el registro de código (app/tools.py).

Los TOOLS se activan por agente desde el dashboard (check en la columna agents.tools),
o por SQL como en el ejemplo comentado al final. No hardcodees la activación aquí."""
import asyncio
import asyncpg
from app.config import settings
from app.tools import catalog


async def main():
    c = await asyncpg.connect(settings.database_url.replace("+asyncpg", ""))
    await c.execute("""
        CREATE TABLE IF NOT EXISTS tools_catalog (
            name TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            description TEXT,
            kind TEXT DEFAULT 'local',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    for t in catalog():
        await c.execute("""
            INSERT INTO tools_catalog(name,label,description) VALUES($1,$2,$3)
            ON CONFLICT(name) DO UPDATE SET label=EXCLUDED.label, description=EXCLUDED.description
        """, t["name"], t["label"], t["description"])
    # EJEMPLO (opcional): activar un tool en un agente por SQL. Descoméntalo y ajusta el
    # slug/tool a los tuyos. Normalmente esto se hace desde el dashboard con un check.
    # await c.execute("""UPDATE agents SET tools='["fecha_hora"]'::jsonb, updated_at=now()
    #                    WHERE slug='ejemplo-vendedor'""")
    print("tools_catalog:", [r["name"] for r in await c.fetch("SELECT name FROM tools_catalog")])
    await c.close()

asyncio.run(main())
