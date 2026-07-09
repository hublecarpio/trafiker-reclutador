"""Role registry — one entry per campaign / avatar.

Cada rol (avatar) lleva:
- trigger_phrases: substrings del autofill del anuncio CTWA (el texto pre-cargado en
  la cajita de WhatsApp) que identifican el rol desde el PRIMER mensaje del lead
  (atribución). El anuncio de cada avatar usa un autofill distinto, así el ruteo es fiable.
- required_docs: lo que el agente debe recolectar (CV, portafolio, fotos, etc.).
- rubric: qué hace fuerte a un candidato/lead — guía la pre-evaluación del agente.
- intro: el primer mensaje (opcional; si vacío, el bot arma uno con la plantilla).
- system_prompt: persona + criterio. Si se define, ANULA la plantilla de reclutamiento.
- extra_context: conocimiento del puesto/producto que se inyecta en el prompt.
- no_fixed_timeline: si True, el agente NO promete plazos en días.

Ruteo: el primer mensaje CTWA es único por campaña (fijas el autofill al lanzar el ad),
así detect_role() resuelve el rol de forma confiable.
"""
from dataclasses import dataclass, field


@dataclass
class Role:
    slug: str
    title: str
    trigger_phrases: list[str]
    required_docs: str
    rubric: str  # what makes a strong candidate — guides the agent's pre-eval
    intro: str = ""
    system_prompt: str = field(default="")
    extra_context: str = ""          # role-specific knowledge injected into the system prompt
    no_fixed_timeline: bool = False  # if True, the agent must NOT promise day-counts


# Días que dura el proceso de evaluación que se le comunica al candidato.
# (También configurable por env EVALUATION_DAYS; aquí es el default de la plantilla.)
_EVAL_DAYS = 14

# Estilo/persona común a los roles que usan la PLANTILLA de reclutamiento (los que
# NO definen system_prompt propio). Reemplaza "[TU EMPRESA]" por el nombre de tu marca.
_COMMON_STYLE = (
    "REGLA ABSOLUTA: NUNCA inventes datos. No inventes direcciones, links/URLs, mapas, montos, fechas, "
    "nombres de contacto ni horarios. Si no tienes un dato exacto en tu contexto, di que el equipo se lo "
    "confirmará por este medio. Es preferible decir 'te lo confirmamos' antes que dar un dato falso.\n\n"
    "Eres el asistente de reclutamiento de [TU EMPRESA]. "
    "Hablas español (tú, tono cálido, profesional y breve) — mensajes de WhatsApp, "
    "no párrafos largos. No prometes el puesto ni das sueldos. Si preguntan por plazos, explicas "
    f"que el proceso de evaluación y selección toma aproximadamente {_EVAL_DAYS} días y que les "
    "avisaremos por aquí. Tu objetivo: dar la bienvenida, confirmar el rol, pedir los documentos "
    "necesarios, resolver dudas básicas y dejar al candidato tranquilo sobre los tiempos. "
    "Nunca inventes datos del cliente final; el equipo gestiona el proceso."
)


def _intro(title: str, ask: str) -> str:
    return (
        f"¡Hola! 👋 Soy del equipo de reclutamiento. Gracias por postular a *{title}*.\n\n"
        f"Para avanzar tu evaluación, {ask}\n\n"
        f"📌 El proceso de evaluación y selección toma aprox. {_EVAL_DAYS} días. "
        "Te escribiremos por aquí con la respuesta, así que tranquilo/a — vamos con calma y con criterio. 🙌"
    )


# ════════════════════════════════════════════════════════════════════════════════
# ROLES — ⚠️ ESTOS SON EJEMPLOS: reemplázalos por TUS roles reales.
#
# Cada rol = un AVATAR con su FRASE-GATILLO (el autofill del anuncio CTWA). El texto
# pre-cargado en la cajita de WhatsApp del anuncio debe contener una de las
# `trigger_phrases` del rol: así el bot sabe a qué avatar enrutar sin endpoint nuevo.
#
# Dos formas de definir un rol:
#   1. RECLUTAMIENTO (sin system_prompt): el bot arma el prompt con build_system_prompt()
#      a partir de title + required_docs + rubric + extra_context. Ver "ejemplo-reclutador".
#   2. VENTAS/CALIFICACIÓN (con system_prompt propio): tú escribes la persona completa y
#      el agente emite el marcador [CALIFICA: ...] cuando el lead es bueno. Ver
#      "ejemplo-vendedor". Los marcadores [CALIFICA:...] avisan al reclutador; [HUMANO:...]
#      hacen handoff a una persona.
#
# Tras editar este dict: `python -m scripts.seed_agents_phase1` y reconstruye el bot.
# ════════════════════════════════════════════════════════════════════════════════

_VENDEDOR_PROMPT = (
    "REGLA ABSOLUTA: NUNCA inventes datos (montos, comisiones, direcciones, links, fechas, nombres). "
    "Si no tienes un dato exacto, di que el equipo se lo confirma por este medio.\n\n"
    "Eres el asistente de reclutamiento de [TU EMPRESA]. Atiendes por WhatsApp a personas que postularon "
    "a un puesto de VENDEDOR/A. Hablas español (tú), cálido, directo y profesional; mensajes cortos de "
    "WhatsApp (1-4 líneas). Tu trabajo: CALIFICAR con criterio (no ruegues, no prometas el puesto).\n\n"
    "QUÉ EVALUAR (pregunta de a poco, conversando, NO todo junto):\n"
    "1) EXPERIENCIA en ventas: qué vendía, dónde, qué metas/resultados lograba.\n"
    "2) ACTITUD COMERCIAL: hambre de cierre, aguante al rechazo, tolerancia al trabajo dinámico.\n"
    "3) DISPONIBILIDAD para el horario del puesto.\n\n"
    "DOCUMENTOS: pide SIEMPRE su CV. Si preguntan por sueldo/comisiones, di que el esquema se detalla en "
    "la entrevista; NO inventes cifras.\n\n"
    "CUANDO CALIFICA (buena experiencia + actitud + disponibilidad): dile que su perfil encaja y que el "
    "equipo lo contactará para coordinar la entrevista. Emite AL FINAL, en su PROPIA línea, el marcador "
    "interno (el candidato NO lo ve):\n"
    "[CALIFICA: nombre | experiencia en ventas | actitud/observaciones | disponibilidad | CV sí/no]\n"
    "Si el caso necesita a una persona real, emite [HUMANO: motivo]."
)

ROLES: dict[str, Role] = {
    # ── EJEMPLO 1: rol de VENTAS con system_prompt propio (emite [CALIFICA: ...]) ──
    "ejemplo-vendedor": Role(
        slug="ejemplo-vendedor",
        title="Vendedor/a (ejemplo)",
        trigger_phrases=[
            "quiero postular a ventas",
            "postulo a vendedor",
            "postulo a ventas",
            "trabajo de ventas",
        ],
        required_docs="tu CV (o cuéntame tu experiencia en ventas) y tu disponibilidad de horario",
        rubric=(
            "Vendedor/a con experiencia comercial demostrable (qué vendía, metas y resultados), fuerte "
            "actitud de cierre y aguante al rechazo, disponible para el horario del puesto. La actitud "
            "comercial pesa por encima de la experiencia. Descartar tibieza o rigidez ante el trabajo dinámico."
        ),
        intro=(
            "¡Hola! 👋 Gracias por tu interés en el puesto de *Vendedor/a*.\n\n"
            "Para avanzar, cuéntame: ¿qué experiencia tienes en ventas y qué resultados lograbas? "
            "Y si puedes, envíame tu *CV* por aquí. 💪"
        ),
        system_prompt=_VENDEDOR_PROMPT,
        no_fixed_timeline=True,
    ),
    # ── EJEMPLO 2: rol de RECLUTAMIENTO sin system_prompt (usa build_system_prompt) ──
    "ejemplo-reclutador": Role(
        slug="ejemplo-reclutador",
        title="Analista de Datos (ejemplo)",
        trigger_phrases=[
            "quiero postular a analista",
            "postulo a analista de datos",
            "analista de datos",
        ],
        required_docs="tu CV + LinkedIn, y si tienes, un proyecto o dashboard del que estés orgulloso/a",
        rubric=(
            "Fuerte: SQL avanzado, Python, alguna herramienta de BI (Power BI/Looker/Metabase), 2-6 años de "
            "experiencia, capacidad de definir KPIs y explicar a no técnicos. Plus: uso real de IA en su flujo."
        ),
        intro=_intro(
            "Analista de Datos",
            "cuéntame: ¿cuántos años de experiencia tienes?, ¿qué tan fuerte es tu SQL/Python?, y "
            "envíame tu CV + LinkedIn por aquí.",
        ),
    ),
}

# Generic fallback when we can't tell the role yet.
GENERIC = Role(
    slug="generic",
    title="una de nuestras vacantes",
    trigger_phrases=[],
    required_docs="tu CV o portafolio y a qué puesto postulas",
    rubric="Desconocido — primero identificar a qué vacante postula.",
    intro=(
        "¡Hola! 👋 Soy del equipo de reclutamiento. Gracias por escribir. "
        "¿A qué puesto te gustaría postular? Cuéntame y te indico los siguientes pasos. 🙌"
    ),
)


def build_system_prompt(role: Role) -> str:
    # roles con prompt propio (ej. ventas/calificación) NO usan la plantilla de reclutamiento
    if (role.system_prompt or "").strip():
        return role.system_prompt
    prompt = (
        f"{_COMMON_STYLE}\n\n"
        f"ROL AL QUE POSTULA: {role.title}.\n"
        f"DOCUMENTOS A PEDIR: {role.required_docs}.\n"
        f"PERFIL IDEAL (para tu evaluación interna, NO lo recites textual): {role.rubric}\n\n"
    )
    if role.no_fixed_timeline:
        prompt += (
            "⚠️ OVERRIDE DE PLAZOS para este rol: IGNORA la regla de los '14 días'. NO menciones "
            "plazos en días exactos. El tiempo de selección depende de la marca/campaña; di que lo "
            "contactaremos cuando corresponda.\n\n"
        )
    if role.extra_context:
        prompt += role.extra_context + "\n\n"
    prompt += (
        "Mantén el hilo de la conversación. Si ya enviaron documentos, agradéceles y confirma que "
        "su postulación quedó registrada. Si falta algo, pídelo amablemente. "
        "Responde SIEMPRE en 1-4 líneas, tono WhatsApp."
    )
    return prompt


def detect_role(message: str) -> Role | None:
    """STRICT phrase gate: the agent only engages if the message contains a known
    campaign trigger phrase. No inbox fallback, no generic catch-all — if nothing
    matches we return None and the bot stays silent (never replies to randoms)."""
    text = (message or "").lower()
    for role in ROLES.values():
        for phrase in role.trigger_phrases:
            if phrase in text:
                return role
    return None
