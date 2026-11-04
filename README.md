# ToolNet ReelShort Metadata API

Self-hosted metadata API for ReelShort language editions. Metadata only — no video URLs, no DRM/stream payloads.

## Features
- **Multi-language**: 22+ locales (en, vi, th, es, fr, de, ja, ko, pt, ru, id, zh-TW, ar, pl, it, tr, ro, cs, bg, tl, zh)
- **Cache-first** with TTL, background refresh, bounded concurrency
- **Edition discovery**: Auto-find language variants of the same story
- **Episode metadata**: Titles, serial numbers, durations, thumbnails (no video URLs)
- **Public API** (`/v1`) + **Admin API** (`/admin`)
- **Rate limiting**, API keys, admin auth
- **SQLite** (local) or **Turso/libSQL** (cloud, recommended for Render)

## Quick Start (Local)

```bash
# 1. Clone & setup
git clone <your-repo>
cd reelshort-api
cp .env.example .env
# Edit .env with your values

# 2. Run with Docker
docker compose up -d --build

# 3. Check health
curl http://127.0.0.1:8077/health
curl http://127.0.0.1:8077/docs
```

## Deploy to Render (Free Tier)

### Prerequisites
1. **GitHub account** - Push this repo to GitHub
2. **Turso account** (free) - `turso db create reelshort-api`
3. **Render account** (free)

### Step 1: Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit: ReelShort Metadata API"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/reelshort-api.git
git push -u origin main
```

### Step 2: Create Turso Database
```bash
# Install Turso CLI: https://docs.turso.tech/cli/installation
turso auth login
turso db create reelshort-api
turso db tokens create reelshort-api
# Save the URL and token
```

### Step 3: Deploy on Render
1. Go to [Render Dashboard](https://dashboard.render.com) → **New** → **Blueprint**
2. Connect your GitHub repo
3. Render detects `render.yaml` → Click **Apply**
4. **Set Environment Variables** (Settings → Environment):
   - `ADMIN_PASSWORD` - Strong password (generate: `openssl rand -base64 32`)
   - `TURSO_DATABASE_URL` - From Turso (e.g., `libsql://reelshort-api-xyz.turso.io`)
   - `TURSO_AUTH_TOKEN` - From Turso token
   - `PUBLIC_API_KEYS` - Comma-separated keys for public API access
5. Click **Save Changes** → Auto-deploys

### Step 4: Verify
```bash
# Health check
curl https://reelshort-api.onrender.com/health

# API docs
open https://reelshort-api.onrender.com/docs

# Test public endpoint (with API key)
curl -H "X-API-Key: your-api-key" https://reelshort-api.onrender.com/v1/movies?lang=en
```

## API Endpoints

### Public (`/v1`) - Requires `X-API-Key` header
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/v1/movies` | List movies (filter: lang, tag, category, q) |
| GET | `/v1/search` | Search local index |
| GET | `/v1/movies/{book_id}` | Movie metadata + editions |
| GET | `/v1/movies/{book_id}/languages` | Available language editions |
| GET | `/v1/movies/{book_id}/episodes` | Episode list (metadata only) |
| GET | `/v1/groups/{group_id}` | Movie group with all verified editions |
| POST | `/v1/discover` | Discover language editions for a movie |
| GET | `/v1/tasks/{task_id}` | Task status (discovery/crawl) |

### Admin (`/admin`) - Requires HTTP Basic Auth (`admin` / `ADMIN_PASSWORD`)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/admin/crawl` | Trigger crawl job |
| POST | `/admin/discover` | Trigger discovery job |
| GET | `/admin/stats` | System statistics |
| GET | `/admin/jobs` | Job queue status |
| POST | `/admin/backup` | Backup database |

## Render Free Tier Limitations

| Limitation | Workaround |
|------------|------------|
| **Sleeps after 15 min inactivity** | Cold start ~30s. Use external cron (cron-job.org, GitHub Actions) to ping `/health` every 10 min |
| **No persistent disk** | **Use Turso** (configured above) - data persists across deploys |
| **512 MB RAM, 0.5 CPU** | Set `WORKER_CONCURRENCY=1`, `SOURCE_REQUESTS_PER_MINUTE=10` |
| **No background workers when sleeping** | Disable `SCHEDULER_ENABLED`, use external cron for scheduled crawls |

## External Cron for Scheduled Crawls (Free)

**GitHub Actions** (`.github/workflows/cron-crawl.yml`):
```yaml
name: Scheduled Catalog Sync
on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours
  workflow_dispatch:

jobs:
  crawl:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger catalog sync (en + vi)
        run: |
          curl -X POST "https://reelshort-api.onrender.com/admin/crawl" \
            -u "admin:${{ secrets.ADMIN_PASSWORD }}" \
            -H "Content-Type: application/json" \
            -d '{"kind": "locale_catalog", "payload": {"lang": "en", "force": false}}'
          curl -X POST "https://reelshort-api.onrender.com/admin/crawl" \
            -u "admin:${{ secrets.ADMIN_PASSWORD }}" \
            -H "Content-Type: application/json" \
            -d '{"kind": "locale_catalog", "payload": {"lang": "vi", "force": false}}'
```

## Local Development

```bash
# With virtualenv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload --port 8077

# Run tests
pytest

# Update test fixtures from live site
python scripts/refresh_fixtures.py
```

## Project Structure
```
├── app/
│   ├── api/           # FastAPI routes (v1, admin, health)
│   ├── crawler/       # Crawl orchestration, parsing, discovery
│   ├── db/            # SQLAlchemy models, base
│   ├── services/      # Business logic (library, jobs, worker)
│   ├── config.py      # Settings (pydantic-settings)
│   ├── main.py        # FastAPI app factory
│   └── logging_config.py
├── migrations/        # Alembic migrations
├── tests/             # Pytest suite + fixtures
├── scripts/           # Backup, restore, repair utilities
├── docker/            # Entrypoint
├── Dockerfile         # Multi-stage production image
├── docker-compose.yml # Local dev stack
├── render.yaml        # Render Blueprint
└── .env.example       # Environment template
```

## License
Internal ToolNet project. Not for public redistribution.