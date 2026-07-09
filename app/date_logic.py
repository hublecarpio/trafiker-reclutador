"""Cálculo DETERMINISTA de fechas para el alquiler (los LLM se equivocan en aritmética
de fechas). Parsea fecha de inicio + duración del texto del cliente y devuelve un
contexto ya calculado para que el agente solo lo relate (no recalcule)."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LIMA = ZoneInfo("America/Lima")
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = {m: i + 1 for i, m in enumerate(
    ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
     "septiembre", "octubre", "noviembre", "diciembre"])}
MIN_DIAS = 3


def _mk_date(day: int, month: int, today: datetime):
    """Construye una fecha con tz Lima; si ya pasó este año, la pasa al año siguiente."""
    try:
        dt = datetime(today.year, month, day, tzinfo=LIMA)
    except ValueError:
        return None
    if dt.date() < today.date():
        dt = dt.replace(year=today.year + 1)
    return dt


def _explicit_range(text: str, today: datetime):
    """Detecta un RANGO de fechas con inicio Y fin explícitos. Devuelve (inicio, fin) o
    (None, None). Cubre: 'del 30 de junio al 2 de julio' (dos meses) y 'del 26 al 29 de
    junio' / '26 al 29 de junio' (un mes)."""
    # dos meses: "30 de junio al 2 de julio"
    m = re.search(r"(\d{1,2})\s*de\s*([a-záéíóú]+)\s*(?:al|a|hasta|y|-|–)\s*(?:el\s*)?"
                  r"(\d{1,2})\s*de\s*([a-záéíóú]+)", text)
    if m and m.group(2) in MESES and m.group(4) in MESES:
        s = _mk_date(int(m.group(1)), MESES[m.group(2)], today)
        e = _mk_date(int(m.group(3)), MESES[m.group(4)], today)
        if s and e and e >= s:
            return s, e
    # un mes: "26 al 29 de junio" / "del 26 al 29 de junio"
    m = re.search(r"(\d{1,2})\s*(?:al|hasta|-|–)\s*(?:el\s*)?(\d{1,2})\s*de\s*([a-záéíóú]+)", text)
    if m and m.group(3) in MESES:
        mo = MESES[m.group(3)]
        s = _mk_date(int(m.group(1)), mo, today)
        e = _mk_date(int(m.group(2)), mo, today)
        if s and e and e >= s:
            return s, e
    return None, None


def _start_date(text: str, today: datetime):
    # "15 de julio"
    m = re.search(r"(\d{1,2})\s*de\s*([a-záéíóú]+)", text)
    if m and m.group(2) in MESES:
        d, mo, y = int(m.group(1)), MESES[m.group(2)], today.year
        try:
            dt = datetime(y, mo, d, tzinfo=LIMA)
        except ValueError:
            return None
        if dt.date() < today.date():
            dt = dt.replace(year=y + 1)
        return dt
    # "15/07" o "15/7/2026"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        try:
            dt = datetime(y, mo, d, tzinfo=LIMA)
        except ValueError:
            return None
        if not m.group(3) and dt.date() < today.date():
            dt = dt.replace(year=y + 1)
        return dt
    return None


def _duration_days(text: str):
    m = re.search(r"(\d+)\s*(?:h|horas?)\b", text)
    if m:
        h = int(m.group(1))
        return h / 24.0, f"{h} horas"
    m = re.search(r"(\d+)\s*(?:d|d[ií]as?)\b", text)
    if m:
        return float(int(m.group(1))), f"{m.group(1)} días"
    m = re.search(r"(\d+)?\s*semanas?\b", text)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        return n * 7.0, f"{n} semana(s)"
    return None, None


def _fmt(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]} {dt.day:02d}/{dt.month:02d}/{dt.year}"


# ---- TARIFAS FIJAS Territory (el modelo NO decide el precio; el sistema lo calcula) ----
TERRITORY_RATE_SHORT = 89   # USD/día, 1-9 días
TERRITORY_RATE_LONG = 79    # USD/día, 10+ días
TERRITORY_LONG_FROM = 10    # el rate largo aplica desde 10 días
TERRITORY_PROMO_EVERY = 6   # cada 6 días alquilados, 1 día gratis


def territory_price(days: float) -> dict | None:
    """Precio FIJO del Territory a partir de los días. Determinista (no lo decide el LLM).
    Devuelve {rate, billable_days, free_days, total, tier} o None si days inválido."""
    if not days or days < 1:
        return None
    full = int(round(days))
    rate = TERRITORY_RATE_LONG if full >= TERRITORY_LONG_FROM else TERRITORY_RATE_SHORT
    free = full // TERRITORY_PROMO_EVERY                 # PROMO: 1 día gratis cada 6
    billable = max(1, full - free)
    return {
        "rate": rate, "days": full, "free_days": free, "billable_days": billable,
        "total": rate * billable,
        "tier": "10+ días" if full >= TERRITORY_LONG_FROM else "1-9 días",
    }


def territory_price_block(days: float, usd_pen: float | None = None) -> str | None:
    """Bloque de contexto con el precio YA calculado para que el agente lo relate tal cual.
    Si `usd_pen` (tipo de cambio venta SUNAT) viene dado, agrega el equivalente en SOLES ya
    calculado, para clientes que no manejan dólares."""
    p = territory_price(days)
    if not p:
        return None
    promo = (f" (PROMO 6 días: {p['free_days']} día(s) gratis → cobras {p['billable_days']} de {p['days']})"
             if p["free_days"] else "")
    soles = ""
    if usd_pen:
        total_pen = round(p["total"] * usd_pen)
        soles = (f" Equivale a **S/ {total_pen}** al tipo de cambio SUNAT del día "
                 f"(1 USD = S/ {usd_pen}); úsalo si el cliente quiere pagar en soles.")
    return (f"- PRECIO FIJO (úsalo TAL CUAL, NUNCA inventes ni cambies la tarifa): "
            f"tarifa USD {p['rate']}/día ({p['tier']}) × {p['billable_days']} día(s) = "
            f"**USD {p['total']} en total** por el alquiler{promo}.{soles} La garantía USD 500 y la "
            f"separación S/100 van aparte.")


def parse_rental_range(text: str, today: datetime | None = None):
    """Devuelve (inicio, fin) como datetimes con tz Lima, o (None, None) si no hay fecha
    parseable. Si hay inicio pero no duración, fin = None. Usado por la agenda de reservas."""
    today = today or datetime.now(LIMA)
    t = (text or "").lower()
    # 1) rango explícito "del 26 al 29 de junio" / "del 30 de junio al 2 de julio"
    rs, re_ = _explicit_range(t, today)
    if rs:
        return rs, re_
    # 2) inicio + duración
    start = _start_date(t, today)
    days, _ = _duration_days(t)
    if not start:
        return None, None
    end = start + timedelta(days=days) if days else None
    return start, end


def rental_date_context(text: str, today: datetime | None = None, vehicle: str | None = None,
                        usd_pen: float | None = None) -> str | None:
    """Devuelve un bloque de contexto con las fechas YA calculadas (y, para el Territory,
    el PRECIO fijo ya calculado), o None si no hay fecha ni duración parseable en el texto.

    `vehicle`: si es 'territory', inyecta la tarifa determinista (89/79 + promo). Para otros
    vehículos (ej. Mazda) NO se inyecta precio (sus tarifas son distintas y van en su prompt)."""
    today = today or datetime.now(LIMA)
    t = (text or "").lower()
    # rango explícito "del 26 al 29 de junio" → inicio y fin ciertos, duración derivada
    rs, re_ = _explicit_range(t, today)
    if rs and re_:
        start = rs
        days = (re_ - rs).days
        dur_label = f"{days} días"
    else:
        start = _start_date(t, today)
        days, dur_label = _duration_days(t)
    if not start and days is None:
        return None

    lines = ["[FECHAS YA CALCULADAS POR EL SISTEMA — úsalas TAL CUAL, no recalcules ni inventes "
             "días de la semana:"]
    if start:
        lines.append(f"- Recojo: {_fmt(start)}")
        # ANTICIPACIÓN: no se entrega el mismo día; mínimo 1 día de anticipación.
        if start.date() <= today.date():
            lines.append("- ⚠️ El recojo pedido es HOY (mismo día). NO se entrega el mismo día: se necesita "
                         "MÍNIMO 1 día de anticipación. NO confirmes hoy; ofrece la entrega a partir de MAÑANA.")
        if days:
            ret = start + timedelta(days=days)
            lines.append(f"- Duración pedida: {dur_label} (= {days:g} días)")
            lines.append(f"- Devolución: {_fmt(ret)} a la MISMA hora de recojo")
    elif days:
        lines.append(f"- Duración pedida: {dur_label} (= {days:g} días). Falta la fecha de recojo; pídela.")

    # Territory: alquila desde 1 día (1-2 días = USD 89/día). Precio FIJO calculado por el sistema,
    # con equivalente en soles al TC SUNAT si se conoce.
    if vehicle == "territory":
        price = territory_price_block(days, usd_pen) if days else None
        if price:
            lines.append(price)
    # Otros vehículos (ej. Mazda) mantienen su mínimo de 3 días y su tarifa propia.
    elif days is not None and days < MIN_DIAS:
        lines.append(f"- ⚠️ {dur_label} es MENOR al mínimo de {MIN_DIAS} días. Avísale con amabilidad que "
                     f"el alquiler mínimo es {MIN_DIAS} días y ofrécele ajustar las fechas.")

    lines.append("Si falta la HORA de recojo, pídela; con la hora, confirma recojo y devolución exactos.]")
    return "\n".join(lines)


# ---- Disponibilidad que el DUEÑO habilita para su unidad (registro de flota) ----
# Devuelve reglas normalizadas para fleet_windows: días de la semana recurrentes (0=lun..6=dom)
# y/o ventanas con fechas concretas. El LLM confirma la interpretación antes de guardar.
_WD_FULL = list(range(7))            # todos los días
_WD_WEEK = [0, 1, 2, 3, 4]           # lun-vie
_WD_WEEKEND = [5, 6]                 # sáb-dom


def parse_availability(text: str, today: datetime | None = None) -> dict:
    """Interpreta lo que el dueño dice sobre cuándo puede alquilar su carro.
    Devuelve {'weekdays': [int], 'windows': [(start,end)], 'raw': text}.
    - 'lun a vie' / 'entre semana' / 'de lunes a viernes'      -> weekdays [0-4]
    - 'fines de semana' / 'findes' / 'sábados y domingos'      -> [5,6]
    - 'todos los días menos findes'                            -> [0-4] (el 'menos finde' gana)
    - 'todos los días' / 'toda la semana' / 'cualquier día'    -> [0-6]
    - 'del 5 al 20 de julio' / 'del 30 de junio al 2 de julio' -> windows (reusa _explicit_range)
    Puede devolver weekdays Y windows a la vez."""
    today = today or datetime.now(LIMA)
    t = (text or "").lower()
    weekdays: list[int] = []
    windows: list[tuple] = []

    # 1) ventana(s) con fechas explícitas
    rs, re_ = _explicit_range(t, today)
    if rs and re_:
        windows.append((rs, re_))

    # 2) reglas recurrentes (orden importa: 'menos finde' y 'entre semana' antes que 'finde')
    has_weekend_word = any(w in t for w in ("finde", "fin de semana", "fines de semana",
                                            "sabado y domingo", "sábado y domingo",
                                            "sabados y domingos", "sábados y domingos"))
    if any(w in t for w in ("menos finde", "menos los finde", "excepto finde", "menos fin de semana",
                            "entre semana", "lun a vie", "lunes a viernes", "de lunes a viernes",
                            "lun-vie", "lun - vie", "días de semana", "dias de semana", "laborables")):
        weekdays = _WD_WEEK
    elif any(w in t for w in ("todos los dias", "todos los días", "toda la semana", "cualquier dia",
                              "cualquier día", "siempre", "todo el tiempo")) and not has_weekend_word:
        weekdays = _WD_FULL
    elif has_weekend_word:
        weekdays = _WD_WEEKEND

    return {"weekdays": weekdays, "windows": windows, "raw": (text or "").strip()}


def availability_echo(av: dict) -> str | None:
    """Texto legible de la disponibilidad parseada, para que el agente CONFIRME su lectura."""
    if not av:
        return None
    parts = []
    if av.get("weekdays") == _WD_WEEK:
        parts.append("de lunes a viernes")
    elif av.get("weekdays") == _WD_WEEKEND:
        parts.append("fines de semana")
    elif av.get("weekdays") == _WD_FULL:
        parts.append("todos los días")
    elif av.get("weekdays"):
        parts.append(", ".join(DIAS[d] for d in av["weekdays"]))
    for (s, e) in av.get("windows", []):
        parts.append(f"del {s.day:02d}/{s.month:02d} al {e.day:02d}/{e.month:02d}")
    if not parts:
        return None
    return "[DISPONIBILIDAD ENTENDIDA: " + " y ".join(parts) + "]"
