# Adding a New API Server

Step-by-step guide for adding a new standalone REST API service to the platform. Follows the pattern established by lakehouse (3002), prbot (3003), and reposearch (3004).

## 1. Pick a Port and Name

| Existing | Port |
|----------|------|
| orchestration | 3001 |
| lakehouse | 3002 |
| prbot | 3003 |
| reposearch | 3004 |

Next available: **3005**. Name should be short, lowercase, no hyphens (used as PM2 process name and directory name).

## 2. Create the Service Directory

```
services/<name>/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, CORS, health, OpenAPI spec
│   ├── config.py            # env var loading (<NAME>_* prefix)
│   ├── db.py                # Motor async MongoDB client (own database)
│   ├── auth.py              # Admin key verification, X-User-Token, X-Dependency-Tokens
│   ├── models.py            # Pydantic request/response schemas
│   ├── routes/
│   │   ├── __init__.py
│   │   └── external.py      # All HTTP handlers (APIRouter prefix /api/v1/<name>)
│   ├── services/
│   │   ├── __init__.py
│   │   └── ...              # Business logic modules
│   └── storage/
│       ├── __init__.py
│       └── s3.py            # If needed — own bucket, own credentials
├── openapi/
│   └── <name>.yaml          # OpenAPI 3.0 spec
├── pyproject.toml
└── Dockerfile
```

## 3. Key Files

### pyproject.toml

```toml
[project]
name = "<name>-api"
version = "1.0.0"
description = "Short description"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "motor>=3.3.0",
    "python-dotenv>=1.0.1",
    # add service-specific deps
]

[project.optional-dependencies]
dev = ["ruff", "pytest", "pytest-asyncio"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

### Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY src/ src/
COPY openapi/ openapi/
ENV PYTHONUNBUFFERED=1
EXPOSE <port>
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "<port>", "--workers", "2"]
```

### src/main.py Pattern

```python
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="<Name> API", version="1.0.0")

# CORS, lifespan (get_db/close_db), include_router, /health, /openapi.yaml
# See any existing service's main.py for the full pattern.
```

### Auth Pattern

Three headers from the bridge:

```
Authorization: Bearer <ADMIN_KEY>           # endpoint protection
X-User-Token: <JWT>                         # user identity (decode, no verify)
X-Dependency-Tokens: {"service": "token"}   # forwarded OAuth tokens
```

Copy `auth.py` from reposearch or lakehouse and change the admin key env var name.

### OpenAPI Spec

Serve at `/api/v1/<name>/openapi.yaml` via a route on the router (not just on the app root). This is the URL the bridge team registers.

## 4. Environment Variables

All prefixed with `<NAME>_` — fully independent from other services.

Required at minimum:
```
<NAME>_ADMIN_KEY=           # endpoint protection
<NAME>_MONGODB_URI=         # own database on shared Atlas cluster
<NAME>_DB_NAME=apifunnel_<name>
```

If using S3:
```
<NAME>_S3_ENDPOINT=
<NAME>_S3_ACCESS_KEY=
<NAME>_S3_SECRET=
<NAME>_S3_REGION=
<NAME>_S3_BUCKET=apifunnel-<name>
```

### Setting Secrets

```bash
# Generate an admin key
python3 -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

# Set GitHub Actions secrets
gh secret set <NAME>_ADMIN_KEY --body "<generated-key>"
gh secret set <NAME>_MONGODB_URI --body "mongodb+srv://.../<db_name>?retryWrites=true&w=majority"
# ... etc

# If using S3, create the bucket:
python3 -c "
import boto3; from botocore.config import Config
client = boto3.client('s3', endpoint_url='...', aws_access_key_id='...', aws_secret_access_key='...', region_name='hel1', config=Config(signature_version='s3v4'))
client.create_bucket(Bucket='apifunnel-<name>')
"
```

### Update .env and .env.example

Add the new section to both files.

## 5. Infrastructure Updates (5 files)

### proxy/Caddyfile + proxy/Caddyfile.dev

Add routing block for the new service:

```
# ── <Name> (Python :<port>) ─────────────────────────────────
handle /api/v1/<name> {
    reverse_proxy localhost:<port>
}
handle /api/v1/<name>/* {
    reverse_proxy localhost:<port>
}
handle /health/<name> {
    rewrite * /health
    reverse_proxy localhost:<port>
}
```

### docker-compose.yml

Add service block:

```yaml
<name>:
  build: services/<name>
  ports:
    - "<port>:<port>"
  env_file: .env
  restart: unless-stopped
```

Add to caddy `depends_on`.

### run_server.py

Add to the `SERVICES` dict:

```python
SERVICES = {
    "lakehouse":   {"port": "3002"},
    "prbot":       {"port": "3003"},
    "reposearch":  {"port": "3004"},
    "<name>":      {"port": "<port>"},
}
```

### .github/workflows/deploy.yml

Four changes:

1. **Outputs** — add `<name>: ${{ steps.check.outputs.<name> }}`

2. **Change detection** — add to both full-deploy blocks and the diff block:
   ```
   echo "<name>=true" >> $GITHUB_OUTPUT
   ```
   ```
   echo "<name>=$(echo "$CHANGED" | grep -qE '^services/<name>/' && echo true || echo false)" >> $GITHUB_OUTPUT
   ```

3. **Deploy condition** — add `needs.detect-changes.outputs.<name> == 'true'` to the `if` clause

4. **Deploy step** — add before "Sync Caddy config":
   ```yaml
   - name: Deploy <name>
     if: needs.detect-changes.outputs.<name> == 'true'
     run: |
       SSH="ssh -i ~/.ssh/key -o StrictHostKeyChecking=no root@$NODE_IP"
       SCP="scp -i ~/.ssh/key -o StrictHostKeyChecking=no -r"
       $SSH "mkdir -p /opt/api-apifunnel/services/<name>"
       $SCP services/<name>/pyproject.toml "root@$NODE_IP:/opt/api-apifunnel/services/<name>/"
       $SCP services/<name>/src "root@$NODE_IP:/opt/api-apifunnel/services/<name>/"
       $SCP services/<name>/openapi "root@$NODE_IP:/opt/api-apifunnel/services/<name>/"
       $SSH "cd /opt/api-apifunnel/services/<name> && pip install --no-cache-dir -q ."
       $SSH "cd /opt/api-apifunnel && pm2 restart <name> --update-env 2>/dev/null || pm2 start 'uvicorn src.main:app --host 0.0.0.0 --port <port> --workers 2' --name <name> --cwd services/<name> --interpreter none --max-memory-restart 512M"
   ```

5. **Env vars** — add to the `.env` write block

6. **Health check** — add:
   ```
   $SSH "curl -sf http://localhost:<port>/health && echo ' ✅ <Name> OK' || (echo ' ❌ <Name> down' && pm2 logs <name> --nostream --lines 30 2>&1 || true)"
   ```

## 6. Ownership Rules

- **Own database** — separate `<NAME>_DB_NAME`, never share collections with other services
- **Own S3 bucket** — separate `<NAME>_S3_BUCKET`, own credentials when possible
- **Own admin key** — separate `<NAME>_ADMIN_KEY`
- **Own env prefix** — all env vars prefixed with `<NAME>_`
- **Zero imports** from other services' code

## 7. Deploy

```bash
# Local dev
python3 run_server.py <name>

# Docker
docker compose up

# Production — push to main triggers CI/CD automatically
git push origin main
```

## 8. Verify

```bash
# Health
curl https://api.apifunnel.ai/health/<name>

# OpenAPI spec
curl https://api.apifunnel.ai/api/v1/<name>/openapi.yaml
```
