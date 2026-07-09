"""Registro de TOOLS por agente (config en la columna agents.tools).

Un tool = una capacidad nombrada y activable por agente (check en el dashboard, NO se
hardcodea). Hay dos clases:
- Tools de LECTURA (run pre-LLM): calculan/consultan un dato duro y lo inyectan al
  contexto antes de que responda el LLM (fechas, disponibilidad de la agenda).
- Tools de ESCRITURA marcador-driven (ej. 'reservar'): no corren en run_tools; se activan
  cuando el LLM emite su marcador ([RESERVAR]) y main.py ejecuta la acción. Se registran
  igual en el catálogo para que aparezcan y se activen desde el dashboard.

Las tools reciben (ctx, session) con la sesión de DB (o None para las que no la usan).
"""
from dataclasses import dataclass
from typing import Callable

from . import agenda
from .date_logic import rental_date_context
from .tipo_cambio import get_sunat_rate


@dataclass
class Tool:
    name: str
    label: str
    description: str
    run: Callable                  # async (ctx, session) -> texto a inyectar (o None)
    marker_driven: bool = False    # True = no corre en run_tools; lo dispara un marcador en main.py


# ---- implementaciones ----
async def _calc_fechas_alquiler(ctx: dict, session) -> str | None:
    # Pasa el vehículo del rol para inyectar el PRECIO fijo cuando corresponde (Territory).
    # Trae el TC SUNAT para que el precio incluya el equivalente en soles (clientes que no
    # manejan dólares). Si la API del TC falla, igual cotiza en USD (usd_pen=None).
    veh = agenda.vehicle_for_role(ctx.get("role_slug", ""))
    rate = await get_sunat_rate()
    usd_pen = rate["venta"] if rate else None
    return rental_date_context(ctx.get("conv_text", ""), vehicle=veh, usd_pen=usd_pen)


async def _registro_flota(ctx: dict, session) -> str | None:
    """LECTURA (captación de flota): interpreta la disponibilidad que el dueño dice ("lun-vie",
    "del 5 al 20 de julio") e inyecta un eco para que el agente CONFIRME su lectura."""
    from .date_logic import parse_availability, availability_echo
    av = parse_availability(ctx.get("conv_text", ""))
    return availability_echo(av)


async def _tipo_cambio(ctx: dict, session) -> str | None:
    """LECTURA: inyecta el tipo de cambio SUNAT del día para cotizar en soles si el cliente
    no maneja dólares (sin depender de que haya fechas todavía)."""
    rate = await get_sunat_rate()
    if not rate:
        return None
    return (f"[TIPO DE CAMBIO SUNAT del día (1 USD = S/ {rate['venta']} venta): si el cliente "
            f"quiere pagar en SOLES, conviértele el total en dólares a soles con esta tasa "
            f"(total USD × {rate['venta']}) y dale el monto en soles. La garantía USD 500 se paga "
            f"con TARJETA DE CRÉDITO (se cobra en dólares automáticamente); la separación S/100 es "
            f"en soles. Nunca digas que solo aceptas dólares: ofrece el equivalente en soles.]")


async def _disponibilidad(ctx: dict, session) -> str | None:
    """LECTURA: inyecta la disponibilidad REAL de las fechas que pide el cliente
    (consulta la agenda rental_bookings + el piso de disponibilidad del vehículo)."""
    if session is None:
        return None
    veh = agenda.vehicle_for_role(ctx.get("role_slug", ""))
    if not veh:
        return None
    return await agenda.availability_context(session, veh, ctx.get("conv_text", ""))


async def _reservar(ctx: dict, session) -> str | None:
    # marker-driven: la reserva la ejecuta main.py al ver [RESERVAR]; aquí no hace nada.
    return None


# ---- catálogo ----
REGISTRY: dict[str, Tool] = {
    "calc_fechas_alquiler": Tool(
        "calc_fechas_alquiler", "Cálculo de fechas (alquiler)",
        "Determinista (Python): a partir de fecha de inicio + duración calcula recojo, "
        "devolución y día de la semana, y avisa si es menor al mínimo. El LLM solo lo relata.",
        _calc_fechas_alquiler),
    "disponibilidad": Tool(
        "disponibilidad", "Disponibilidad (agenda de slots)",
        "Antes de responder, consulta la agenda de reservas del vehículo y le dice al bot si "
        "las fechas pedidas están LIBRES u OCUPADAS (y respeta la fecha desde la que está disponible). "
        "Evita que ofrezca días ya reservados.",
        _disponibilidad),
    "reservar": Tool(
        "reservar", "Reservar al depositar (bloquea slots)",
        "Cuando el cliente manda el voucher / confirma el depósito (marcador [RESERVAR]), BLOQUEA "
        "esas fechas en la agenda y avisa al dueño para que verifique el pago en el banco. Solo "
        "reserva si hubo depósito.",
        _reservar, marker_driven=True),
    "tipo_cambio": Tool(
        "tipo_cambio", "Tipo de cambio SUNAT (cotizar en soles)",
        "Trae el tipo de cambio SUNAT del día e inyecta el precio en SOLES para clientes que no "
        "manejan dólares. Evita perder leads por 'solo cobran en dólares': el bot da el monto en "
        "soles al TC oficial. (El cálculo de fechas también incluye el total en soles.)",
        _tipo_cambio),
    "registro_flota": Tool(
        "registro_flota", "Disponibilidad de flota (interpreta días/fechas del dueño)",
        "Captación de flota: lee lo que el dueño dice sobre cuándo puede alquilar su carro "
        "(lun-vie, fines de semana, del 5 al 20 de julio) e inyecta un eco para que el agente "
        "confirme su interpretación antes de registrar la unidad.",
        _registro_flota),
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
