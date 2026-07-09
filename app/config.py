"""Central config, loaded from environment (.env in dev, real env in prod).

Los defaults de abajo son PLACEHOLDERS genéricos. En producción TODO valor real
(URLs, tokens, teléfonos, IDs) llega por variables de entorno (.env). Nunca pongas
un secreto real aquí — este archivo se versiona en git.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Postgres. Host 'postgres' resuelve dentro de docker-compose; en prod usa tu host real.
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/recruitment"

    # Chatwoot
    chatwoot_url: str = "https://chats.example.com"
    chatwoot_account_id: str = "1"
    chatwoot_token: str = ""

    # LLM via OpenRouter (OpenAI-compatible). If empty, agent falls back to templated replies.
    openrouter_api_key: str = ""
    openrouter_base: str = "https://openrouter.ai/api/v1"
    llm_model: str = "openai/gpt-4.1-mini"

    # Redis (acumulador de inputs / debounce)
    redis_url: str = "redis://redis:6379/0"
    # Segundos que el bot espera acumulando mensajes antes de responder (humanizador)
    debounce_seconds: int = 15
    # Delay base entre mensajes al responder partido (simula tipeo humano)
    typing_delay_min: float = 1.5
    typing_delay_max: float = 4.0

    # Notificaciones al reclutador (su WhatsApp). Ponlo con código de país, ej. "+51999999999".
    recruiter_phone: str = "+51999999999"
    # Inbox de Chatwoot desde el que se notifican las citas
    chatwoot_inbox_id: str = "1"

    # Agendamiento de entrevistas
    # sede -> slots diarios (hora de inicio, lunes a viernes)
    interview_slots: str = "10:00,10:30"   # 20 min c/u → máx 2 por día
    interview_sede_enabled: str = "sede-principal"  # sedes con agenda activa
    # Recordatorio automático la noche previa: hora (24h, hora local) en que se confirma la cita del día siguiente
    reminder_hour: int = 20   # 8 pm

    # Behaviour
    evaluation_days: int = 14
    # Comma-separated phone->role default mapping, e.g. "+51999999999:ejemplo-reclutador"
    inbox_role_map: str = ""

    # Webhook hardening (optional shared secret in querystring ?token=)
    webhook_secret: str = ""

    # URL del dashboard (para linkear la ficha del candidato en los avisos al personal)
    dash_url: str = "https://panel.example.com"

    # Handoff con respuesta: base pública donde el dueño escribe la respuesta cuando el bot no sabe.
    answer_base_url: str = "https://app.example.com"

    # Capa de entendimiento de media: audio→Whisper (OpenAI), imagen→Gemini (vision).
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Leadgen opcional desde un formulario de Meta (lead ads). Deja vacío si no lo usas.
    meta_token: str = ""      # token Graph para leer leads del formulario
    autos_form_id: str = ""   # ID del formulario de Meta
    autos_page_id: str = ""   # ID de la página dueña del formulario
    autos_template: str = ""  # nombre de la plantilla de WhatsApp para el primer contacto
    autos_role: str = ""      # slug del rol que atiende esos leads

    # Evolution API (WhatsApp Web/Baileys sobre un WhatsApp PERSONAL).
    # Sirve para recontactar candidatos FUERA de la ventana de 24h sin gastar plantillas:
    # el "asistente reclutador" escribe desde el personal y el dueño confirma humanamente.
    evolution_url: str = ""        # ej. https://wsp.example.com
    evolution_key: str = ""        # apikey de la instancia
    evolution_instance: str = ""   # ej. instancia_personal


settings = Settings()
