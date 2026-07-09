"""Data model for the recruitment funnel.

applicants     — one per WhatsApp contact (per role they applied to)
conversations  — maps a Chatwoot conversation to an applicant + role
messages        — every inbound/outbound message (audit + agent context)
documents       — CVs / files the candidate sent (stored on disk + recorded)
"""
from datetime import datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Applicant(Base):
    __tablename__ = "applicants"

    id: Mapped[int] = mapped_column(primary_key=True)
    chatwoot_contact_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)
    role_slug: Mapped[str] = mapped_column(String(64), index=True)
    # sede / zona a la que aplica (ej: independencia | chorrillos) — el agente la pregunta
    sede: Mapped[str | None] = mapped_column(String(64), index=True)
    # aprobación del reclutador para ofrecer entrevista: NULL=pendiente, True=aprobado, False=rechazado
    # SOLO el reclutador (desde su WhatsApp personal) puede aprobar. Sin aprobación NO se agenda.
    approved_for_interview: Mapped[bool | None] = mapped_column()
    # ¿ya se notificó al reclutador que llegó el CV/portafolio? (evita avisos duplicados)
    recruiter_notified: Mapped[bool] = mapped_column(default=False, server_default="false")
    # stage: new | collecting_docs | docs_received | informed_timeline | in_review | interview_scheduled | done
    stage: Mapped[str] = mapped_column(String(32), default="new")
    # agent's running pre-evaluation notes / score for HR
    eval_score: Mapped[int | None] = mapped_column(Integer)
    eval_notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="applicant")
    documents: Mapped[list["Document"]] = relationship(back_populates="applicant")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    chatwoot_conversation_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"))
    role_slug: Mapped[str] = mapped_column(String(64))
    # Handoff humano: si True, el bot NO responde (el dueño atiende). Se activa desde el panel
    # o automáticamente cuando el agente emite [HUMANO] / el cliente pide una persona.
    bot_paused: Mapped[bool] = mapped_column(default=False, server_default="false")
    pause_reason: Mapped[str | None] = mapped_column(Text)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    applicant: Mapped["Applicant"] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    direction: Mapped[str] = mapped_column(String(16))  # incoming | outgoing
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class PersonalMessage(Base):
    """Mensajes del WhatsApp PERSONAL del dueño (nueva cuenta Chatwoot + Evolution).
    Solo para VER respuestas de candidatos que escriben al personal (ej. confirmaciones)
    cuando la plantilla del número oficial falla o no llega."""
    __tablename__ = "personal_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    contact_name: Mapped[str | None] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(16))  # incoming | outgoing
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Interview(Base):
    """Entrevistas agendadas por el agente. El agente ofrece slots, el candidato
    confirma, se guarda aquí y se notifica al WhatsApp personal del reclutador."""
    __tablename__ = "interviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"), index=True)
    role_slug: Mapped[str | None] = mapped_column(String(64), index=True)  # campaña: agenda compartida
    sede: Mapped[str] = mapped_column(String(64))           # independencia | chorrillos (solo informativo)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # status: scheduled | confirmed | done | cancelled | no_show
    status: Mapped[str] = mapped_column(String(32), default="scheduled")
    notified: Mapped[bool] = mapped_column(default=False)        # ¿ya se avisó al reclutador?
    reminder_sent: Mapped[bool] = mapped_column(default=False)   # ¿ya se mandó el recordatorio la noche previa?
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RentalBooking(Base):
    """Agenda de reservas de los vehículos en alquiler. El bot consulta esta tabla
    antes de ofrecer fechas (no agenda días ya reservados) y crea un 'hold' cuando un
    lead va a separar. El dueño confirma/cancela desde scripts/territory_agenda.py."""
    __tablename__ = "rental_bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    vehicle: Mapped[str] = mapped_column(String(64), index=True)  # ej: territory | mazda-cx5
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # status: hold (separación pendiente de confirmar) | confirmed (pagada) | cancelled
    status: Mapped[str] = mapped_column(String(16), default="hold", index=True)
    applicant_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    # Marketplace de flota: a qué unidad del inventario corresponde la reserva (nullable por compat).
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("fleet_units.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FleetUnit(Base):
    """INVENTARIO canónico del marketplace de flota: cada vehículo real (propio de TuEmpresa o de un
    dueño que lo dio en consignación). Une oferta (lo origina un lead capta-flota) y demanda
    (agenda.py y el matcher lo leen). Territory/Mazda viven aquí como filas sembradas."""
    __tablename__ = "fleet_units"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)            # territory | mazda-cx5 | unit-<id>
    owner_applicant_id: Mapped[int | None] = mapped_column(ForeignKey("applicants.id"))
    source_conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    owner_name: Mapped[str | None] = mapped_column(String(255))
    owner_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    make: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    transmission: Mapped[str | None] = mapped_column(String(32))
    year: Mapped[int | None] = mapped_column(Integer)
    km: Mapped[int | None] = mapped_column(Integer)
    price_ref_usd: Mapped[float | None] = mapped_column(Numeric)
    daily_rate_usd: Mapped[float | None] = mapped_column(Numeric)         # NULL = delega en date_logic
    deposit_usd: Mapped[float | None] = mapped_column(Numeric, default=500)
    min_days: Mapped[int | None] = mapped_column(Integer, default=3)
    commission_pct: Mapped[float | None] = mapped_column(Numeric)         # reparto % de TuEmpresa
    zone: Mapped[str | None] = mapped_column(String(128))
    city: Mapped[str | None] = mapped_column(String(64), default="Lima")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|approved|active|paused|rejected
    ownership: Mapped[str | None] = mapped_column(String(16), default="consignment")  # own|consignment
    drive_folder_id: Mapped[str | None] = mapped_column(String(128))
    drive_folder_url: Mapped[str | None] = mapped_column(Text)
    drive_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    photos_count: Mapped[int | None] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Escalation(Base):
    """Handoff con RESPUESTA: cuando el bot no sabe algo (marcador [ESCALAR]), crea un registro
    con un token. Al dueño le llega un link (portal) donde escribe la respuesta; el bot la recoge,
    la pule con IA y se la manda al cliente. Mientras, la conversación queda pausada (callada)."""
    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"))
    chatwoot_conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    role_slug: Mapped[str | None] = mapped_column(String(64))
    client_name: Mapped[str | None] = mapped_column(String(255))
    client_phone: Mapped[str | None] = mapped_column(String(32))
    question: Mapped[str | None] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending|answered|sent
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FleetWindow(Base):
    """Ventanas de disponibilidad que el DUEÑO habilita para su unidad (estilo Airbnb/canal).
    'available' = días/rango habilitado; 'blackout' = el dueño lo bloqueó."""
    __tablename__ = "fleet_windows"

    id: Mapped[int] = mapped_column(primary_key=True)
    fleet_unit_id: Mapped[int] = mapped_column(ForeignKey("fleet_units.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="available")   # available | blackout
    start_date: Mapped[datetime | None] = mapped_column(Date)
    end_date: Mapped[datetime | None] = mapped_column(Date)
    weekdays: Mapped[str | None] = mapped_column(String(20))             # CSV 0-6 (lun..dom) recurrente
    price_override_usd: Mapped[float | None] = mapped_column(Numeric)
    raw_text: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(16), default="owner")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicants.id"), index=True)
    kind: Mapped[str | None] = mapped_column(String(32))  # cv | portfolio | image | other
    file_name: Mapped[str | None] = mapped_column(String(512))
    source_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    # Flota: a qué unidad del inventario pertenece esta foto (las fotos del lead-dueño).
    unit_id: Mapped[int | None] = mapped_column(ForeignKey("fleet_units.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    applicant: Mapped["Applicant"] = relationship(back_populates="documents")
