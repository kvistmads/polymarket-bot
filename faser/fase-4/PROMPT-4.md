# Fase 4 — Filter Scanner CLI

## Kontekst

Du arbejder i `polymarket-bot` mappen på branch `fase-4-filter` (opret den fra main).

Fase 1 (database), Fase 2 (monitor) og Fase 3 (executor) er færdige og mergede til main.
Nu skal du bygge `filter.py` — et manuelt CLI-værktøj til at score og udvælge wallets.

**Dette er IKKE en del af den automatiske pipeline.** Filter-systemet bruges manuelt til at
vedligeholde listen af fulgte wallets, score nye kandidater og justere konfiguration.

**Læs disse filer FØR du skriver én linje kode:**
```
Read CLAUDE.md
Read PRD.md
Read .ecc/postgres-patterns/SKILL.md
Read .ecc/tdd-workflow/SKILL.md
Read .ecc/backend-patterns/SKILL.md
Read .ecc/rules/python/coding-style.md
Read db.py
```

---

## Mål

Opret `filter.py` (max 300 linjer — split i moduler hvis nødvendigt) og `tests/test_filter.py`.

---

## Trin 1 — Branch

```bash
git checkout main
git pull
git checkout -b fase-4-filter
```

---

## Trin 2 — CLI-struktur med argparse

`filter.py` eksponerer disse subkommandoer:

```bash
python filter.py scan   <wallet_address>           # Score én wallet
python filter.py list   [--min-sortino 1.2]        # List fulgte wallets med scores
python filter.py follow <wallet_address> [--label "navn"] [--size-pct 0.07]
python filter.py unfollow <wallet_address> [--reason "tekst"]
python filter.py recalculate                       # Genberegn alle scores + gem snapshot
```

Implementér med `argparse` og subparsers. Hvert subkommando kalder sin egen async funktion.

Indgangspunkt:
```python
if __name__ == "__main__":
    args = build_parser().parse_args()
    asyncio.run(args.func(args))
```

---

## Trin 3 — `scan` kommando

`scan` henter historiske trades for én wallet fra Polymarket Data API og beregner alle metrics.

```python
async def cmd_scan(args: argparse.Namespace) -> None:
    """Scan og score én wallet. Skriv resultater til stdout + gem i wallet_scores."""
```

**Data-hentning:**
```
GET https://data-api.polymarket.com/activity?user={address}&limit=500
```

Gentag med `offset` pagination indtil ingen flere resultater (rate-limit: 0.5 req/s — brug `await asyncio.sleep(2)` mellem kald).

**Hvad der gemmes:**
- Opret wallet i `wallets` tabel hvis den ikke eksisterer (INSERT ON CONFLICT DO NOTHING)
- Gem beregnede scores i `wallet_scores` (INSERT ON CONFLICT (wallet_id) DO UPDATE)
- Gem altid nyt snapshot i `wallet_score_snapshots`

**Output til stdout:**
```
Wallet: 0xABC...  (label hvis sat)
Trades total:     142
Win rate:         61.3%
Sortino ratio:    1.87
Max drawdown:     23.4%
Bull win rate:    63.1%  |  Bear win rate: 58.2%
Consistency:      92.1%
Sizing entropy:   0.74
Est. bankroll:   $4,821
ÅOP:             +187.3%
Last scored:      2026-04-29 14:32
```

---

## Trin 4 — Score-beregning

Implementér `calculate_scores(trades: list[dict]) -> dict` i en separat hjælpefunktion.

### Metrics

**Win rate:**
```python
won = sum(1 for t in trades if float(t.get("cashPnl", 0)) > 0)
win_rate = won / len(trades) if trades else 0
```

**Sortino ratio** (downside deviation, annualiseret):
```python
returns = [float(t.get("percentPnl", 0)) / 100 for t in trades]
downside = [r for r in returns if r < 0]
downside_std = statistics.stdev(downside) if len(downside) > 1 else 0.001
avg_return = statistics.mean(returns) if returns else 0
# Annualisér: antag 52 trades/år som approximation
sortino = (avg_return * 52) / (downside_std * (52 ** 0.5)) if downside_std else 0
```

**Max drawdown:**
```python
# Kumulativ P&L — find største fald fra peak til trough
cumulative = list(itertools.accumulate(float(t.get("cashPnl", 0)) for t in trades))
peak = cumulative[0]
max_dd = 0.0
for val in cumulative:
    peak = max(peak, val)
    if peak > 0:
        dd = (peak - val) / peak
        max_dd = max(max_dd, dd)
```

**Bull/Bear win rate:**
Polymarket API returnerer `outcome` ('Yes'/'No') og `cashPnl`.
- Bull trades = køb af 'Yes' tokens
- Bear trades = køb af 'No' tokens

```python
bull = [t for t in trades if t.get("outcome", "").lower() == "yes"]
bear = [t for t in trades if t.get("outcome", "").lower() == "no"]
bull_win_rate = sum(1 for t in bull if float(t.get("cashPnl", 0)) > 0) / len(bull) if bull else None
bear_win_rate = sum(1 for t in bear if float(t.get("cashPnl", 0)) > 0) / len(bear) if bear else None
```

**Consistency score** (bull vs bear divergens):
```python
if bull_win_rate is not None and bear_win_rate is not None:
    consistency = 1.0 - abs(bull_win_rate - bear_win_rate)
else:
    consistency = None
```

**Sizing entropy** (position størrelse variation — høj entropy = ikke ensartet):
```python
import math
sizes = [float(t.get("size", 0)) for t in trades if float(t.get("size", 0)) > 0]
if sizes:
    total = sum(sizes)
    probs = [s / total for s in sizes]
    entropy = -sum(p * math.log2(p) for p in probs if p > 0)
    # Normalisér til 0-1 (max entropy = log2(n))
    max_entropy = math.log2(len(sizes)) if len(sizes) > 1 else 1
    sizing_entropy = entropy / max_entropy if max_entropy > 0 else 0
else:
    sizing_entropy = None
```

**Estimated bankroll** (sum af current value for åbne positioner):
```python
# Hent fra positions-tabel for denne wallet (hvis tracked)
# Fallback: sum af size × curPrice fra scan-data
```

**ÅOP (annual return pct):**
```python
# Total P&L / estimated_bankroll * (365 / antal_dage_i_historik)
total_pnl = sum(float(t.get("cashPnl", 0)) for t in trades)
# Brug first_trade dato til at estimere historik-længde
```

---

## Trin 5 — `list` kommando

```python
async def cmd_list(args: argparse.Namespace) -> None:
    """List alle aktive fulgte wallets med deres seneste wallet_scores."""
```

SQL:
```sql
SELECT w.address, w.label, ws.win_rate, ws.sortino_ratio,
       ws.max_drawdown, ws.trades_total, ws.last_scored_at,
       fw.position_size_pct, fw.followed_at
FROM followed_wallets fw
JOIN wallets w ON w.id = fw.wallet_id
LEFT JOIN wallet_scores ws ON ws.wallet_id = fw.wallet_id
WHERE fw.unfollowed_at IS NULL
ORDER BY ws.sortino_ratio DESC NULLS LAST
```

Filtrer på `--min-sortino` hvis angivet.

Output som tabel (brug `tabulate` eller simpel formateret string — tilføj `tabulate>=0.9` til requirements.txt).

---

## Trin 6 — `follow` kommando

```python
async def cmd_follow(args: argparse.Namespace) -> None:
    """Tilføj wallet til followed_wallets. Opretter wallet-record hvis nødvendigt."""
```

Trin:
1. `INSERT INTO wallets (address, label) VALUES ($1, $2) ON CONFLICT (address) DO UPDATE SET label = EXCLUDED.label`
2. Tjek at wallet ikke allerede er aktivt fulgt (unfollowed_at IS NULL)
3. `INSERT INTO followed_wallets (wallet_id, position_size_pct) VALUES ($1, $2)`
4. Print bekræftelse: `✅ Følger nu [label] (0xABC…) med size_pct=7.0%`

Valider `--size-pct`: skal være mellem 0.01 og 0.20 (1%–20%). Fejl ved ugyldig værdi.

---

## Trin 7 — `unfollow` kommando

```python
async def cmd_unfollow(args: argparse.Namespace) -> None:
    """Sæt unfollowed_at på aktiv followed_wallets-række. Sletter ALDRIG data."""
```

SQL:
```sql
UPDATE followed_wallets
SET unfollowed_at = now(), reason = $2
WHERE wallet_id = (SELECT id FROM wallets WHERE address = $1)
  AND unfollowed_at IS NULL
```

Print bekræftelse eller fejl hvis wallet ikke følges.

---

## Trin 8 — `recalculate` kommando

```python
async def cmd_recalculate(args: argparse.Namespace) -> None:
    """Genberegn scores for alle aktive fulgte wallets + gem snapshots."""
```

Henter liste af aktive wallets fra `followed_wallets`, kalder `cmd_scan` logikken for hver
(rate-limit: 0.5 req/s = `await asyncio.sleep(2)` mellem wallets).

Gem et nyt snapshot i `wallet_score_snapshots` for hver wallet — uanset om scores har ændret sig.

Print progress: `[1/3] Scanning 0xABC… (whale-001)…`

---

## Trin 9 — Miljøvariable

```python
load_dotenv()
DB_URL: str = os.environ["DB_URL"]
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
DATA_API = "https://data-api.polymarket.com"
```

Ingen `POLYMARKET_PRIVATE_KEY` behøves i filter.py — det er kun monitor + executor der bruger den.

---

## Trin 10 — Tests (tests/test_filter.py)

Skriv minimum 10 tests. Ingen rigtige DB- eller HTTP-kald.

```python
# Score-beregning (4 tests)
def test_win_rate_basic():
    """Win rate beregnes korrekt fra trades med positive/negative cashPnl."""

def test_sortino_ratio_positive_for_good_trader():
    """Sortino er positiv for wallet med overvejende positive returns."""

def test_max_drawdown_zero_for_only_gains():
    """Max drawdown er 0 hvis alle trades er profitable."""

def test_sizing_entropy_low_for_uniform_sizes():
    """Sizing entropy er lav (< 0.3) for ensartede trade-størrelser."""

# CLI-kommandoer (4 tests — mock DB og HTTP)
async def test_follow_inserts_to_followed_wallets():
    """cmd_follow indsætter korrekt i wallets + followed_wallets."""

async def test_follow_rejects_invalid_size_pct():
    """cmd_follow fejler hvis --size-pct er udenfor 0.01–0.20."""

async def test_unfollow_sets_unfollowed_at():
    """cmd_unfollow sætter unfollowed_at og reason korrekt."""

async def test_list_filters_by_min_sortino():
    """cmd_list filtrerer wallets med sortino under --min-sortino."""

# Data-hentning (2 tests)
async def test_scan_paginates_activity_api():
    """scan henter næste page hvis første returnerer 500 resultater."""

async def test_recalculate_rate_limits_between_wallets():
    """recalculate venter 2 sekunder mellem wallet-scans."""
```

---

## Trin 11 — requirements.txt opdatering

Tilføj:
```
tabulate>=0.9.0
```

---

## Trin 12 — Pre-commit checks

```bash
ruff check . --fix
black .
mypy filter.py db.py --ignore-missing-imports
pytest tests/test_filter.py -x -q
```

Alle 4 skal passere inden commit.

---

## Trin 13 — Commits

```
feat(filter): add CLI scaffold with argparse subcommands
feat(filter): implement calculate_scores (win_rate, sortino, drawdown, entropy, ÅOP)
feat(filter): add scan command — fetch activity API + write wallet_scores + snapshot
feat(filter): add follow/unfollow commands with validation
feat(filter): add list command with tabulate output and --min-sortino filter
feat(filter): add recalculate command with rate limiting
feat(deps): add tabulate to requirements.txt
test(filter): add 10 unit tests covering scores, CLI commands, pagination
```

---

## Trin 14 — RESULT.md

Opret `faser/fase-4/RESULT.md` med:
- Filstruktur og linjeantal
- Kommando-oversigt med eksempel-output
- Score-metrik forklaring (hvad beregner vi og hvorfor)
- Testresultater (10+ tests, alle grønne)
- Verifikationstabel (ruff/black/mypy/pytest)
- Afvigelser fra denne prompt

Opdater `CLAUDE.md` Fase-status: sæt `[x]` på Fase 4.

---

## Vigtige constraints

- `filter.py` er et **manuelt CLI-værktøj** — det kører ikke som daemon
- Brug `Decimal` til alle penge-beregninger, `float` kun til statistik-beregninger (sortino, entropy)
- Brug `TIMESTAMPTZ` — aldrig `TIMESTAMP`
- Skriv ALDRIG til `trade_events` eller `positions` fra filter.py — kun til `wallets`, `followed_wallets`, `wallet_scores`, `wallet_score_snapshots`
- Rate-limit mod Polymarket Data API: max 0.5 req/s (`await asyncio.sleep(2)` mellem kald)
- Brug `httpx` (async) til alle HTTP-kald — ikke `requests`
- Funktioner max ~50 linjer
