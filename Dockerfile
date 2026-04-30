FROM python:3.11-slim

WORKDIR /app

# Installer system-afhængigheder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Opret non-root bruger (docker-patterns best practice)
RUN groupadd -g 1001 botgroup && useradd -u 1001 -g botgroup -s /bin/bash botuser

# Kopier requirements først (cache-optimering — kun rebuild ved ændringer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopier kode og sæt ejerskab
COPY --chown=botuser:botgroup . .

# Kør som non-root
USER botuser

# Ingen CMD her — defineres per service i docker-compose.yml
