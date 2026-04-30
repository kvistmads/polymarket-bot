# Fase 5: Docker + Hetzner Deployment — RESULT

**Status:** ✅ Færdig  
**Branch:** `fase-5-deploy`  
**Dato:** 2026-04-30

---

## Oprettede filer

| Fil | Formål |
|-----|--------|
| `Dockerfile` | Fælles image for monitor + executor. Python 3.11-slim, non-root user (`botuser`) |
| `.dockerignore` | Ekskluderer .git, .env, tests, .ecc m.fl. fra image |
| `docker-compose.yml` | Orkestrerer postgres:16-alpine + monitor + executor med healthchecks |
| `.env.example` | Komplet template med alle env vars og kommentarer |
| `scripts/deploy.sh` | Hetzner deploy: git pull → build → migrate → up |
| `scripts/backup.sh` | PostgreSQL backup til gzip-fil, rydder op > 7 dage |

---

## Hetzner Server Setup (én gang)

### Forudsætninger
- Hetzner CX22 oprettet i **Ashburn, Virginia** datacenter (us-east-1 nærhed til Polymarket CLOB)
- SSH-adgang som root

### 1. SSH ind og installer Docker

```bash
ssh root@<SERVER_IP>

# Installer Docker (officielt script)
curl -fsSL https://get.docker.com | sh

# Installer Docker Compose plugin
apt-get install -y docker-compose-plugin

# Verificér installation
docker --version
docker compose version
```

### 2. Opret dedikeret bruger

```bash
useradd -m -s /bin/bash botuser
# Tilføj til docker-gruppen (kan køre docker uden sudo)
usermod -aG docker botuser
mkdir -p /home/botuser/polymarket-bot
chown botuser:botuser /home/botuser/polymarket-bot
```

### 3. Klon repo og konfigurér

```bash
su - botuser
git clone https://github.com/kvistmads/polymarket-bot.git polymarket-bot
cd polymarket-bot

# Opret .env fra template
cp .env.example .env
nano .env   # Udfyld ALLE værdier — se checkliste nedenfor
```

### 4. Kør første deployment

```bash
# Stadig som botuser i /home/botuser/polymarket-bot
bash scripts/deploy.sh
```

Deploy-scriptet udfører i rækkefølge:
1. `git pull origin main`
2. `docker compose build --no-cache`
3. `docker compose run --rm monitor python -m alembic upgrade head`
4. `docker compose up -d`

---

## Alembic Migrations på Hetzner

**VIGTIGT:** Kør aldrig `alembic upgrade head` direkte mod en database udefra Cowork-sandboxen. Det foregår inde i Docker-containeren på Hetzner:

```bash
# Kør migrations mod production database (inde i Docker-netværket)
docker compose run --rm monitor python -m alembic upgrade head
```

Dette starter en engangs-container med `monitor`-image, kører alle 11 migrations mod PostgreSQL-service og afslutter. Output bør vise:

```
INFO  [alembic.runtime.migration] Running upgrade ... -> 001, create wallets
INFO  [alembic.runtime.migration] Running upgrade 001 -> 002, create followed_wallets
...
INFO  [alembic.runtime.migration] Running upgrade 010 -> 011, create daily_stats
```

Hvis der opstår fejl: `docker compose logs postgres` for at se PostgreSQL-status.

---

## Firewall Setup (ufw)

```bash
# Åbn kun SSH — luk ALT andet
ufw allow ssh
ufw enable
ufw status

# Forventet output:
# Status: active
# To                         Action      From
# --                         ------      ----
# 22/tcp                     ALLOW       Anywhere
```

**Ingen åbne porte til omverdenen.** `/health` endpoints (8080, 8081) er kun tilgængelige internt i Docker-netværket — ikke eksponeret til host.

PostgreSQL er heller ikke eksponeret — kun tilgængeligt via `postgres` hostname inde i Docker-compose-netværket.

---

## Backup Procedure

### Manuel backup

```bash
cd /home/botuser/polymarket-bot
bash scripts/backup.sh
# Output: ✅ Backup gemt: /root/polymarket-backups/polymarket_20260430_030000.sql.gz
```

Backups gemmes i `~/polymarket-backups/` og ryddes op efter 7 dage automatisk.

### Daglig automatisk backup (crontab)

```bash
# Rediger crontab som botuser
crontab -e

# Tilføj denne linje:
0 3 * * * cd /home/botuser/polymarket-bot && bash scripts/backup.sh >> /var/log/polymarket-backup.log 2>&1
```

Backups kører kl. 03:00 UTC hver nat.

### Restore fra backup

```bash
# Stop services
docker compose down

# Start kun postgres
docker compose up -d postgres

# Restore
gunzip -c ~/polymarket-backups/polymarket_<TIMESTAMP>.sql.gz | \
  docker compose exec -T postgres psql -U bot polymarket

# Genstart alle services
docker compose up -d
```

---

## Daglig deploy workflow

```bash
# På Hetzner som botuser:
cd /home/botuser/polymarket-bot
bash scripts/deploy.sh
```

Scriptet er idempotent — køres det uden ændringer bygges blot image'ne igen og services genstarter (< 10 sekunder nedetid).

---

## Nyttige kommandoer

```bash
# Se logs
docker compose logs -f monitor
docker compose logs -f executor
docker compose logs --tail=100 postgres

# Tjek services
docker compose ps

# Kør filter-scanner manuelt
docker compose run --rm monitor python filter.py list

# Genstart én service
docker compose restart executor

# Stop alt
docker compose down

# Slet alt inkl. data (DESTRUKTIVT)
docker compose down -v
```

---

## Go-Live Checklist

Afkryds i rækkefølge før live trading aktiveres:

```
[ ] Hetzner CX22 server oprettet (Ashburn datacenter, us-east-1)
[ ] SSH-adgang bekræftet (ssh botuser@<SERVER_IP>)
[ ] Docker + Docker Compose installeret (docker --version ≥ 24)
[ ] Repo klonet: /home/botuser/polymarket-bot
[ ] .env udfyldt med alle værdier (cp .env.example .env)
[ ] POLYMARKET_PRIVATE_KEY sat korrekt i .env (0x... format)
[ ] DRY_RUN=true i .env — bekræft inden deploy
[ ] POSTGRES_PASSWORD sat til stærk, unik adgangskode
[ ] alembic upgrade head kørt — alle 11 migrations OK (ingen fejl)
[ ] docker compose up -d — alle 3 services kører (docker compose ps)
[ ] Health endpoints svarer OK:
    - monitor: curl http://localhost:8080/health → "ok"
    - executor: curl http://localhost:8081/health → "ok"
[ ] Telegram-bot sender test-besked (start monitor og vent ét poll-interval)
[ ] filter.py follow <wallet> kørt med mindst én wallet:
    docker compose run --rm monitor python filter.py follow 0x... --label "whale-001"
[ ] Paper trading kører — copy_orders tabel fyldes med status='paper':
    docker compose exec postgres psql -U bot polymarket -c "SELECT COUNT(*) FROM copy_orders WHERE status='paper';"
[ ] Daglig backup crontab opsat (crontab -e)
[ ] ufw firewall aktiv (ufw status → active, kun SSH åben)
[ ] AgentShield scan bestået (ingen kritiske fund):
    npx agentshield scan .
[ ] Win rate > 52% over ≥20 paper trades → Telegram go-live approval afventes
[ ] DRY_RUN sættes til false KUN via Telegram inline keyboard (ikke manuelt)
```

---

## Sikkerhed — påmindelser

- `POLYMARKET_PRIVATE_KEY` gemmes KUN i `.env` på serveren — aldrig i kode, DB eller logs
- `.env` er i `.gitignore` — verificér med `git status` inden push
- Alle ordrer logges i `copy_orders` uanset DRY_RUN — ingen tavse fejl
- Rotér `POLYMARKET_PRIVATE_KEY` ved mistanke om eksponering
- Kør AgentShield-scan inden første live deploy: `npx agentshield scan .`
