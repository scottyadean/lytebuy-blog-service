# lytebuy-blog-service

Blog posts and press releases for the Lytebuy marketing site. Python 3.12 on
the Serverless Framework, backed by MongoDB Atlas via pymongo.

Posts and press releases share **one collection and one document shape**. The
`content_type` enum (`post` | `press_release`) is the only thing separating
them, and it is set by the route a request arrives on, so a press release can
never be read or written through a `/posts` route or vice versa.

## Endpoints

| Method | Path                          | API key | Notes                                  |
|--------|-------------------------------|---------|----------------------------------------|
| GET    | `/health`                     | no      | Pings mongo; 503 when it is unreachable |
| GET    | `/posts`                      | no      | Newest first, `limit` (max 100) + `offset` |
| GET    | `/posts/{post_id}`            | yes     | Full body, increments `views`          |
| POST   | `/posts`                      | yes     | Create                                 |
| PUT    | `/posts/{post_id}`            | yes     | Partial update                         |
| GET    | `/press-releases`             | no      | Same shape as `/posts`                 |
| GET    | `/press-releases/{post_id}`   | yes     | Full body, increments `views`          |
| POST   | `/press-releases`             | yes     | Create                                 |
| PUT    | `/press-releases/{post_id}`   | yes     | Partial update                         |

The list endpoints are public because the marketing site's blog and press
index pages call them anonymously. Everything else is `private: true` in
`serverless.yml`, which means **API Gateway enforces the `x-api-key` header
from the usage plan before the request reaches Lambda**. No handler does its
own key check.

Routes are declared individually rather than as one `/{proxy+}` catch-all
precisely so the public reads can stay open while the writes stay guarded.

### List response

```json
{
  "items": [ { "id": "...", "title": "...", "views": 12, "created_date": "..." } ],
  "count": 1,
  "total": 37,
  "limit": 100,
  "offset": 0
}
```

`items` omits `body` — the list never ships full article markdown. `total` is
the unpaged count so the site can render pagination without a second call.

### Document shape

```
id  content_type  title  slug  excerpt  body  author
cover_image_url  tags[]  views  created_date  updated_date
```

`content_type` and `views` are **not** caller-writable. The route owns the
former; letting a caller set the latter would make the counter a lie. `views`
is incremented in the same round trip as the single-item read.

`slug` is unique per content type (compound unique index), so a post and a
press release may share a slug — they live under different site routes. A
collision returns `409`.

## Layout

```
src/router.py    route map + dispatch; owns path-param extraction
src/posts.py     handlers, shared by both content types
src/health.py    liveness
src/utils.py     mongo client, validation, serialization, responses
config/*.yml     per-stage env vars, same style as the other sls services
tests/           mongomock-backed handler and router tests
```

## Local development

```bash
npm install
python3 -m venv .venv && .venv/bin/pip install -r requirements.development.txt
cp .env.example .env    # then fill in the real connection string
./.server.sh            # sls offline --noAuth on :5600
```

`--noAuth` skips API-key enforcement locally, so every route is reachable
without a key while developing.

```bash
curl localhost:5600/local/posts
curl -X POST localhost:5600/local/posts \
  -H 'content-type: application/json' \
  -d '{"title":"Why local wins","slug":"why-local-wins","body":"...","author":"Scott D"}'
```

## Tests

```bash
.venv/bin/python -m pytest
```

Tests run against `mongomock`, including the unique index, so the `409`
duplicate-slug path is exercised rather than assumed. No network, no Atlas.

## Configuration

`config/<stage>.yml` holds the env vars, matching the style used by the other
sls services. Stages: `local`, `development`, `production`.

| Var              | Purpose                                                 |
|------------------|---------------------------------------------------------|
| `DB_URL`         | Atlas connection string, may contain `((db_name))`      |
| `DB_NAME`        | `lytebuy-<env>`, matching the Atlas databases           |
| `ALLOWED_ORIGIN` | CORS origin; `*` outside production                     |

`DB_URL` may carry a `((db_name))` placeholder, which `utils.resolve_uri`
substitutes with `DB_NAME` at runtime. One stored secret therefore serves
every stage, and a stale copied URI cannot point production at development
data.

The brackets are round, not curly, for a concrete reason: **AWS Param Store
rejects any value containing `{{}}`**, reading it as a nested parameter
reference.

**Secrets.** `.env` is gitignored and is local-only. Deployed stages resolve
`DB_URL` from AWS Param Store at deploy time:

```
/lytebuy/<env>/blog-service/db-url
```

Store the value with `((db_name))` left intact. Note that the Serverless
Framework resolves `${ssm:...}` with the *deployer's* credentials at deploy
time and bakes the result into the Lambda's environment, so the function
itself needs no `ssm:GetParameter` permission.

## Deploy

```bash
sls deploy --stage development
sls deploy --stage production
```
