# Fase 5 — Docker + Hetzner Deployment

## Kontekst

Du arbejder i `polymarket-bot` mappen på branch `fase-5-deploy` (opret den fra main).

Fase 1–4 er færdige og mergede til main:
- Fase 1: Database (Alembic, db.py)
- Fase 2: monitor.py
- Fase 3: executor.py (5 moduler)
- Fase 4: filter.py (3 moduler)

Nu skal du bygge Docker Compose setup og Hetzner deployment-guide.
**Ingen kode der rammer produktion i denne fase — kun infra-konfiguration og dokumentation.**

**Læs disse filer FØR du skriver én linje kode:**
```
Read CLAUDE.md
Read PRD.md
Read .ecc/docker-patterns/SKILL.md
Read .ecc/rules/common/security.md
```

---

## Mål

Opret følgende filer og dokumentation:

```
polymarket-bot/
├── Dockerfile
├── docker-compose.yml
├── .env.example            (opdater med alle nye env vars)
├── scripts/
│   ├── deploy.sh           (Hetzner: git pull + compose up)
│   └── backup.sh           (PostgreSQL backup til lokal fil)
└── faser/fase-5/
    └── RESULT.md
```

---

## Trin 1 — Branch

```bash
git checkout main
git pull
git checkout -b fase-5-deploy
```

---

## Trin 2 — Dockerfile

Én enkelt Dockerfile der bruges af alle tre services (monitor, executor, filter).

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Installer system-afhængigheder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Kopier requirements først (cache-optimering)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopier kode
COPY . .

# Ingen CMD her — defineres per service i docker-compose.yml
```

Tilføj `.dockerignore`:
```
.git
.env
.env.*
__pycache__
*.pyc
*.pyo
.mypy_cache
.ruff_cache
tests/
faser/
*.sh
```

---

## Trin 3 — docker-compose.yml

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    volumes:
      - postgres_data:/var/lib/postgresql/data
    environment:
      POSTGRES_DB: polymarket
      POSTGRES_USER: bot
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U bot -d polymarket"]
      interval: 10s
      timeout: 5s
      retries: 5

  monitor:
    build: .
    command: python monitor.py
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DB_URL: postgresql+asyncpg://bot:${POSTGRES_PASSWORD}@postgres/polymarket
      FOLLOWED_WALLETS: ${FOLLOWED_WALLETS}
      POLL_INTERVAL: ${POLL_INTERVAL:-30}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3

  executor:
    build: .
    command: python executor.py
    restart: unless-stopped
    depends_on:
      postgres:
        condition: service_healthy
    environment:
      DB_URL: postgresql+asyncpg://bot:${POSTGRES_PASSWORD}@postgres/polymarket
      POLYMARKET_PRIVATE_KEY: ${POLYMARKET_PRIVATE_KEY}
      MAX_DAILY_LOSS: ${MAX_DAILY_LOSS:-50}
      POSITION_SIZE_PCT: ${POSITION_SIZE_PCT:-0.05}
      DRY_RUN: ${DRY_RUN:-true}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8081/health"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  postgres_data:
```

---

## Trin 4 — .env.example (opdater eksisterende)

Erstat den eksisterende `.env.example` med komplet version:

```bash
# === Database ===
DB_URL=postgresql+asyncpg://bot:CHANGE_ME@localhost/polymarket
POSTGRES_PASSWORD=CHANGE_ME

# === Wallets ===
FOLLOWED_WALLETS=0x...,0x...

# === Monitor ===
POLL_INTERVAL=30

# === Executor ===
POLYMARKET_PRIVATE_KEY=0x...       # Aldrig commit denne!
MAX_DAILY_LOSS=50                   # USD — stop trading hvis daglig tab overstiger dette
POSITION_SIZE_PCT=0.05              # 5% af tilgængeligt cash per trade
DRY_RUN=true                        # Sæt til false KUN via Telegram go-live approval

# === Telegram ===
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# === Generelt ===
LOG_LEVEL=INFO

# === ECC ===
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=65
```

---

## Trin 5 — Alembic upgrade (VIGTIGT)

Fase 5 er det første tidspunkt `alembic upgrade head` køres mod en rigtig database.

Tilføj en sektion i RESULT.md der forklarer præcis hvordan dette gøres på Hetzner:

```bash
# Kør migrations mod production database
docker compose run --rm monitor python -m alembic upgrade head
```

Dette kører Alembic inside containeren mod PostgreSQL-servicen.

---

## Trin 6 — scripts/deploy.sh

```bash
#!/bin/bash
# deploy.sh — Opdater og genstart bot på Hetzner
# Kør som: bash scripts/deploy.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

echo "==> Henter seneste kode fra GitHub..."
git pull origin main

echo "==> Bygger Docker images..."
docker compose build --no-cache

echo "==> Kører database migrations..."
docker compose run --rm monitor python -m alembic upgrade head

echo "==> Genstarter services..."
docker compose up -d

echo "==> Status:"
docker compose ps

echo "✅ Deploy færdig!"
```

---

## Trin 7 — scripts/backup.sh

```bash
#!/bin/bash
# backup.sh — Tag PostgreSQL backup til lokal fil
# Kør som: bash scripts/backup.sh
set -euo pipefail

BACKUP_DIR="$HOME/polymarket-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="polymarket_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "==> Tager backup..."
docker compose exec postgres pg_dump -U bot polymarket | gzip > "$BACKUP_DIR/$FILENAME"

echo "✅ Backup gemt: $BACKUP_DIR/$FILENAME"

# Slet backups ældre end 7 dage
find "$BACKUP_DIR" -name "*.sql.gz" -mtime +7 -delete
echo "==> Gamle backups ryddet op (> 7 dage)"
```

---

## Trin 8 — Hetzner setup guide (i RESULT.md)

Skriv en trin-for-trin guide i RESULT.md med disse præcise kommandoer:

### Server setup (én gang)

```bash
# 1. SSH ind på serveren
ssh root@<SERVER_IP>

# 2. Installer Docker
curl -fsSL https://get.docker.com | sh

# 3. Installer Docker Compose plugin
apt-get install -y docker-compose-plugin

# 4. Opret bruger og mappe
useradd -m -s /bin/bash botuser
mkdir -p /home/botuser/polymarket-bot
chown botuser:botuser /home/botuser/polymarket-bot

# 5. Klon repo
su - botuser
git clone https://github.com/kvistmads/polymarket-bot.git polymarket-bot
cd polymarket-bot

# 6. Opret .env fra template
cp .env.example .env
nano .env   # Udfyld alle værdier

# 7. Kør første deployment
bash scripts/deploy.sh
```

### Firewall setup

```bash
# Åbn kun SSH og luk alt andet
ufw allow ssh
ufw enable
# Ingen åbne porte til omverdenen — /health endpoints er kun interne
```

### Daglig backup (crontab)

```bash
# Tilføj til crontab: crontab -e
0 3 * * * cd /home/botuser/polymarket-bot && bash scripts/backup.sh >> /var/log/polymarket-backup.log 2>&1
```

---

## Trin 9 — Pre-commit checks

```bash
# Ingen Python-filer at checke i denne fase — kun config
# Valider docker-compose syntax:
docker compose config --quiet && echo "✅ docker-compose.yml valid"
```

Hvis `docker` ikke er tilgængeligt i udviklingsmiljøet, skip dette og noter det i RESULT.md.

---

## Trin 10 — RESULT.md

Opret `faser/fase-5/RESULT.md` med:
- Filstruktur og hvad hver fil gør
- Komplet Hetzner setup guide (copy-paste klar)
- `alembic upgrade head` procedure
- Backup-procedure
- Firewall-setup
- Daglig backup via crontab
- Hvad der er nødvendigt FØR go-live (checklist)

**Go-live checklist i RESULT.md:**
```
[ ] Hetzner CX22 server oprettet (Ashburn datacenter)
[ ] SSH-adgang bekræftet
[ ] Docker installeret
[ ] Repo klonet og .env udfyldt
[ ] POLYMARKET_PRIVATE_KEY sat i .env
[ ] DRY_RUN=true i .env (bekræft)
[ ] alembic upgrade head kørt (alle 11 migrations OK)
[ ] docker compose up -d (alle 3 services kører)
[ ] /health endpoints svarer OK (monitor :8080, executor :8081)
[ ] Telegram-bot sender test-besked
[ ] filter.py follow <wallet> kørt med mindst én wallet
[ ] Paper trading kører (copy_orders fyldes med status='paper')
[ ] AgentShield scan: npx agentshield scan . (ingen kritiske fund)
```

Opdater `CLAUDE.md` Fase-status: sæt `[x]` på Fase 5.

---

## Trin 11 — Commits

```
feat(deploy): add Dockerfile and .dockerignore
feat(deploy): add docker-compose.yml with postgres, monitor, executor services
feat(deploy): add scripts/deploy.sh and scripts/backup.sh
chore: update .env.example with all required env vars
docs(fase-5): add RESULT.md with Hetzner setup guide and go-live checklist
```

---

## Vigtige constraints

- `POLYMARKET_PRIVATE_KEY` og `POSTGRES_PASSWORD` må **aldrig** i docker-compose.yml — kun via env vars fra `.env`
- `.env` er i `.gitignore` — bekræft dette inden push
- `DRY_RUN=true` skal være default i docker-compose.yml — aldrig `false`
- Kør IKKE `alembic upgrade head` fra Cowork-sandboxen — kun på Hetzner med rigtig DB
- `scripts/` mappen oprettes som en del af denne fase
