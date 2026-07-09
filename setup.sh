#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
#  setup.sh — prepara el .env y autogenera los secretos del panel.
#  Idempotente: puedes correrlo las veces que quieras; NUNCA pisa valores ya puestos.
# ══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")"

# 1) Si no existe .env, cópialo de la plantilla.
if [ ! -f .env ]; then
  cp .env.template .env
  echo "📄 Creado .env a partir de .env.template"
else
  echo "📄 .env ya existe — no lo sobrescribo (idempotente)."
fi

# gen_secret KEY LONGITUD  → si KEY está vacía en .env, le pone un valor aleatorio.
gen_secret() {
  local key="$1" bytes="$2"
  # ¿Existe la línea "KEY=..." y su valor (antes de un posible comentario) está vacío?
  local current
  current="$(grep -E "^${key}=" .env | head -n1 | sed -E "s/^${key}=//; s/[[:space:]]*#.*$//; s/[[:space:]]*$//")"
  if [ -z "${current}" ]; then
    local val
    val="$(openssl rand -hex "${bytes}")"
    if grep -qE "^${key}=" .env; then
      # Reemplaza SOLO el valor, conservando cualquier comentario en la línea.
      sed -i -E "s|^(${key}=)[^#]*(#.*)?$|\1${val}   \2|" .env
    else
      printf '%s=%s\n' "${key}" "${val}" >> .env
    fi
    echo "🔐 ${key} autogenerado."
  else
    echo "✅ ${key} ya tenía valor — lo conservo."
  fi
}

gen_secret DASH_PASS 12
gen_secret WEBHOOK_SECRET 12

cat <<'EOF'

✅ Secretos del panel generados. Ahora llena SOLO estas keys externas en .env:
   HIGGSFIELD_API_KEY, META_TOKEN, META_AD_ACCOUNT_ID, OPENAI_API_KEY,
   GEMINI_API_KEY, OPENROUTER_API_KEY, CHATWOOT_URL/ACCOUNT_ID/TOKEN/INBOX_ID,
   RECRUITER_PHONE.

Luego: docker compose up -d
EOF
