"""Registro de TOOLS por agente (config en la columna agents.tools).

Un tool = una capacidad nombrada y activable por agente (check en el dashboard, NO se
hardcodea). Hay dos clases:
- Tools de LECTURA (run pre-LLM): calculan/consultan un dato duro y lo inyectan al
  contexto antes de que responda el LLM (ej. la fecha/hora local).
- Tools de ESCRITURA marcador-driven: no corren en run_tools; se activan cuando el LLM
  emite su marcador (ej. `[ALGO]`) y main.py ejecuta la acción. Se registran igual en el
  catálogo para que aparezcan y se activen desde el dashboard.

Las tools reciben (ctx, session) con la sesión de DB (o None para las que no la usan).

⚠️ El único tool que viene aquí (`fecha_hora`) es un EJEMPLO neutral para que veas la forma
de un tool de lectura. Agrega los tuyos siguiendo ese patrón y siémbralos con
`python -m scripts.seed_tools`.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo


@dataclass
class Tool:
    name: str
    label: str
    description: str
    run: Callable                  # async (ctx, session) -> texto a inyectar (o None)
    marker_driven: bool = False    # True = no corre en run_tools; lo dispara un marcador en main.py


# ---- implementaciones ----
async def _fecha_hora(ctx: dict, session) -> str | None:
    """LECTURA (ejemplo): inyecta la fecha y hora local para que el agente no invente
    fechas relativas ('mañana', 'este viernes'). Determinista, no depende del LLM."""
    tz = ZoneInfo("America/Lima")
    now = datetime.now(tz)
    dias = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    return (f"[FECHA/HORA ACTUAL (úsala tal cual): {dias[now.weekday()]} "
            f"{now.strftime('%d/%m/%Y %H:%M')} (hora Lima). No inventes fechas relativas.]")


# ---- catálogo ----
REGISTRY: dict[str, Tool] = {
    "fecha_hora": Tool(
        "fecha_hora", "Fecha y hora local",
        "Inyecta la fecha y hora local del día antes de que responda el agente, para que "
        "no invente fechas relativas. Tool de LECTURA de ejemplo (copia este patrón).",
        _fecha_hora),
}


async def run_tools(enabled: list[str] | None, ctx: dict, session=None) -> str:
    """Corre los tools de LECTURA activos del agente y junta su salida. Los marker-driven se
    saltan (los dispara main.py). Un tool que falla NO rompe la respuesta."""
    out = []
    for name in (enabled or []):
        tool = REGISTRY.get(name)
        if not tool or tool.marker_driven:
            continue
        try:
            r = await tool.run(ctx, session)
            if r:
                out.append(r)
        except Exception:
            pass
    return "\n".join(out)


def catalog() -> list[dict]:
    return [{"name": t.name, "label": t.label, "description": t.description} for t in REGISTRY.values()]
