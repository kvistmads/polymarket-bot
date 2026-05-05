#!/bin/bash
# watchdog.sh — Tjekker container-sundhed og genstarter ved fejl
# Kør via cron hvert 30. minut:
#   */30 * * * * /home/botuser/polymarket-bot/watchdog.sh >> /var/log/polymarket-watchdog.log 2>&1

set -euo pipefail

COMPOSE_DIR="/home/botuser/polymarket-bot"
LOG_PREFIX="[watchdog $(date '+%Y-%m-%d %H:%M:%S')]"

# Hent Telegram-tokens fra .env
if [[ -f "$COMPOSE_DIR/.env" ]]; then
    export $(grep -E '^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID)=' "$COMPOSE_DIR/.env" | xargs)
fi

TELEGRAM_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT="${TELEGRAM_CHAT_ID:-}"

send_telegram() {
    local msg="$1"
    if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT" ]]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/sendMessage" \
            -H "Content-Type: application/json" \
            -d "{\"chat_id\": \"${TELEGRAM_CHAT}\", \"text\": \"${msg}\", \"parse_mode\": \"HTML\"}" \
            > /dev/null
    fi
}

echo "$LOG_PREFIX Starter sundhedstjek..."

cd "$COMPOSE_DIR"

# Hent status for alle containers
UNHEALTHY=$(docker compose ps --format json 2>/dev/null \
    | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
bad = []
for line in lines:
    if not line.strip():
        continue
    try:
        c = json.loads(line)
        health = c.get('Health', '')
        state  = c.get('State', '')
        name   = c.get('Name', c.get('Service', '?'))
        if health == 'unhealthy' or state not in ('running', ''):
            bad.append(f'{name}({health or state})')
    except Exception:
        pass
print('\n'.join(bad))
" 2>/dev/null || echo "")

if [[ -z "$UNHEALTHY" ]]; then
    echo "$LOG_PREFIX Alle containers raske ✅"
    exit 0
fi

echo "$LOG_PREFIX UNHEALTHY: $UNHEALTHY — genstarter..."

# Genstart kun de syge services
RESTARTED=()
while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    # Udtræk service-navn (før parentes)
    SERVICE=$(echo "$line" | sed 's/(.*//')
    # Fjern container-prefix (polymarket-bot-)
    SERVICE="${SERVICE#polymarket-bot-}"
    # Fjern -1 suffix
    SERVICE="${SERVICE%-1}"
    echo "$LOG_PREFIX Genstarter service: $SERVICE"
    docker compose restart "$SERVICE" 2>&1 || true
    RESTARTED+=("$SERVICE")
done <<< "$UNHEALTHY"

sleep 10

# Tjek om genstart hjalp
STILL_UNHEALTHY=$(docker compose ps --format json 2>/dev/null \
    | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
bad = []
for line in lines:
    if not line.strip():
        continue
    try:
        c = json.loads(line)
        health = c.get('Health', '')
        state  = c.get('State', '')
        name   = c.get('Name', c.get('Service', '?'))
        if health == 'unhealthy' or state not in ('running', ''):
            bad.append(name)
    except Exception:
        pass
print(' '.join(bad))
" 2>/dev/null || echo "")

RESTARTED_STR=$(IFS=', '; echo "${RESTARTED[*]}")

if [[ -z "$STILL_UNHEALTHY" ]]; then
    echo "$LOG_PREFIX Genstart lykkedes ✅"
    send_telegram "🔄 <b>Watchdog: Automatisk genstart</b>
Service: ${RESTARTED_STR}
Problem: ${UNHEALTHY}
Status: ✅ Løst
Tid: $(date '+%d/%m %H:%M UTC')"
else
    echo "$LOG_PREFIX STADIG UNHEALTHY efter genstart: $STILL_UNHEALTHY ⚠️"
    send_telegram "⚠️ <b>Watchdog: Genstart FEJLEDE</b>
Service: ${RESTARTED_STR}
Stadig nede: ${STILL_UNHEALTHY}
Tid: $(date '+%d/%m %H:%M UTC')
Manuel indgriben påkrævet!"
fi
