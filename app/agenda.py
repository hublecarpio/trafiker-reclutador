"""Agenda de reservas de vehículos en alquiler.

- availability_context(): mira la tabla rental_bookings y devuelve un bloque [AGENDA ...]
  que se inyecta al contexto del agente ANTES del LLM, para que nunca ofrezca fechas ya
  reservadas ni anteriores a que el vehículo esté disponible.
- add_hold(): crea una reserva tentativa (status 'hold') cuando un lead va a separar.

Hoy hay un solo vehículo (territory) y recién disponible desde el 25/06/2026, pero todo
está parametrizado por `vehicle` y fecha de disponibilidad para sumar más carros luego.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .date_logic import parse_rental_range
from .models import RentalBooking

LIMA = ZoneInfo("America/Lima")

# Estados que BLOQUEAN un día: 'reserved' = voucher recibido (pendiente de verificar en banco),
# 'confirmed' = el dueño ya validó el depósito. 'cancelled' no bloquea.
BLOCKING = ("reserved", "confirmed")


def _floor_from_env(var: str, default: str) -> datetime:
    """Lee una fecha de disponibilidad 'YYYY-MM-DD' del entorno (el dueño la actualiza sin
    re-deploy). Si no es válida o no existe, usa el default."""
    raw = (os.getenv(var) or default).strip()
    try:
        y, m, d = (int(x) for x in raw.split("-"))
        return datetime(y, m, d, tzinfo=LIMA)
    except Exception:
        y, m, d = (int(x) for x in default.split("-"))
        return datetime(y, m, d, tzinfo=LIMA)


# Fecha mínima de ENTREGA por vehículo (antes de eso el carro aún no está disponible).
# DINÁMICO: configurable por entorno para que el dueño lo actualice sin tocar código.
#   RENTAL_TERRITORY_AVAILABLE_FROM=2026-07-01 (formato YYYY-MM-DD)
AVAILABLE_FROM = {
    "territory": _floor_from_env("RENTAL_TERRITORY_AVAILABLE_FROM", "2026-07-02"),
    "mazda-cx5": _floor_from_env("RENTAL_MAZDA_AVAILABLE_FROM", "2026-06-25"),
}

# Qué vehículo (agenda) maneja cada rol de alquiler. Sumar carros = agregar aquí.
ROLE_VEHICLE = {"alquiler-territory": "territory", "alquiler-mazda-cx5": "mazda-cx5"}


def vehicle_for_role(slug: str) -> str | None:
    return ROLE_VEHICLE.get(slug)


def _fmt(dt: datetime) -> str:
    return f"{dt.day:02d}/{dt.month:02d}/{dt.year}"


def _active_floor(vehicle: str) -> datetime | None:
    """Piso de disponibilidad SOLO si todavía es relevante (en el futuro). Si la fecha de
    disponibilidad ya pasó, el carro está disponible hoy → no hay piso que aplicar."""
    floor = AVAILABLE_FROM.get(vehicle)
    if floor and floor.date() > datetime.now(LIMA).date():
        return floor
    return None


async def availability_context(session, vehicle: str, text: str) -> str | None:
    """Bloque de contexto con la disponibilidad REAL de las fechas que pide el cliente.
    None si no hay fecha parseable en el texto."""
    start, end = parse_rental_range(text)
    floor = _active_floor(vehicle)
    if not start:
        # sin fecha aún: igual recordamos la disponibilidad mínima si existe
        if floor:
            return (f"[AGENDA: el vehículo recién está disponible para entrega desde el {_fmt(floor)}. "
                    f"No agendes recojos antes de esa fecha; aún no preguntan fecha, pero tenlo presente.]")
        return None

    end = end or start
    lines = ["[AGENDA — disponibilidad REAL (úsala tal cual, NO inventes):"]

    # 1) ¿antes de que el carro esté disponible?
    if floor and start < floor:
        lines.append(f"- ⚠️ Piden recojo el {_fmt(start)}, pero el vehículo recién está disponible desde "
                     f"el {_fmt(floor)}. NO confirmes esa fecha; ofrece desde el {_fmt(floor)} en adelante.")
        lines.append("]")
        return "\n".join(lines)

    # 2) ¿choca con otra reserva (hold o confirmada)?
    rows = (await session.execute(
        select(RentalBooking).where(
            RentalBooking.vehicle == vehicle,
            RentalBooking.status.in_(BLOCKING),
        )
    )).scalars().all()
    conflict = None
    for b in rows:
        # solapamiento de rangos [start,end] ∩ [b.start,b.end]
        if start <= b.end_date and b.start_date <= end:
            conflict = b
            break

    if conflict:
        lines.append(f"- ⚠️ Las fechas {_fmt(start)}–{_fmt(end)} CHOCAN con una reserva existente "
                     f"({_fmt(conflict.start_date)}–{_fmt(conflict.end_date)}). NO confirmes esas fechas; "
                     f"discúlpate con amabilidad y ofrece otras libres (solo tenemos un vehículo).")
    else:
        lines.append(f"- ✅ Las fechas {_fmt(start)}–{_fmt(end)} están LIBRES. Puedes avanzar al cierre.")
    lines.append("]")
    return "\n".join(lines)


async def reserve(session, vehicle: str, text: str, phone: str | None, note: str | None):
    """Reserva (BLOQUEA) las fechas SOLO cuando el cliente ya hizo el depósito / mandó voucher.
    Estado 'reserved' (bloquea, pendiente de que el dueño verifique en el banco). Devuelve el
    RentalBooking creado (con id) o None si no hay fecha parseable, es antes del piso, o choca."""
    start, end = parse_rental_range(text)
    if not start:
        return None
    end = end or start
    floor = _active_floor(vehicle)
    if floor and start < floor:
        return None
    rows = (await session.execute(
        select(RentalBooking).where(
            RentalBooking.vehicle == vehicle,
            RentalBooking.status.in_(BLOCKING),
        )
    )).scalars().all()
    for b in rows:
        if start <= b.end_date and b.start_date <= end:
            return None
    booking = RentalBooking(vehicle=vehicle, start_date=start, end_date=end,
                            status="reserved", applicant_phone=phone, note=note)
    session.add(booking)
    await session.flush()   # asigna id para referenciarlo en la alerta al dueño
    return booking
