# lytebuy-blog-service

Blog posts and press releases for the Lytebuy marketing site. Python 3.12 on
the Serverless Framework, backed by **S3**: each record is one JSON object in a
per-stage bucket.

Posts and press releases share **one document shape**. The `content_type` enum
(`post` | `press_release`) is the only thing separating them, and it maps to the
S3 key prefix the record is stored under (`posts/`, `press-releases/`). It is set
by the route a request arrives on, so a press release can never be read or written
through a `/posts` route or vice versa - the prefix in the key makes cross-type
access impossible, not just forbidden.

## Storage model

Every record is a JSON object at `{prefix}/{uuid}.json`:

```
posts/{uuid}.json
press-releases/{uuid}.json
featured/{uuid}.json
```

All fields live inside the JSON body **except the view count**, which is stored in
S3 object metadata as `x-amz-meta-view-count`. Keeping views out of the body means
a view bump copies the object onto itself to change one metadata value and never
rewrites the article.

There is no query engine: listing reads every object under a prefix and sorts /
filters in memory, and slug uniqueness is enforced on write by scanning the prefix.
This is fine at blog scale (hundreds of records); if volume grows into the
thousands, a manifest or slug-index object becomes the next optimization.

## Endpoints

| Method | Path                          | API key | Notes                                  |
|--------|-------------------------------|---------|----------------------------------------|
| GET    | `/health`                     | no      | `head_bucket`; 503 when the bucket is unreachable |
| GET    | `/posts`                      | no      | Newest first, `limit` (max 100) + `offset` |
| GET    | `/posts/{post_id}`            | no      | Full body. Does **not** bump `views`   |
| POST   | `/posts`                      | yes     | Create                                 |
| PUT    | `/posts/{post_id}`            | yes     | Partial update                         |
| POST   | `/posts/{post_id}/views`      | no      | Bump the view counter, returns `{ "views": n }` |
| GET    | `/press-releases`             | no      | Same shape as `/posts`                 |
| GET    | `/press-releases/{post_id}`   | no      | Full body. Does **not** bump `views`   |
| POST   | `/press-releases`             | yes     | Create                                 |
| PUT    | `/press-releases/{post_id}`   | yes     | Partial update                         |
| POST   | `/press-releases/{post_id}/views` | no  | Bump the view counter                  |
| GET    | `/featured`                   | no      | The currently-active featured vendor (empty when none) |
| GET    | `/featured/{feature_id}`      | yes     | One feature by id (admin/preview)      |
| POST   | `/featured`                   | yes     | Create a featured-vendor spotlight     |
| PUT    | `/featured/{feature_id}`      | yes     | Partial update                         |

Reads are public. **Writes** (create/update) require the `x-api-key` header, which
API Gateway enforces from the usage plan before the request reaches Lambda
(`private: true` in `serverless.yml`). The view-bump endpoints are public because
the host app calls them from the browser after a page loads.

### View counting

A single-item GET no longer increments the counter. Instead the **host app** (the
marketing site) calls `POST /{type}/{id}/views` after a post page loads. The bump
is server-side (`head_object` -> read count -> `copy_object` onto self with the
incremented value) and returns the new count. It is not atomic, so two exactly
simultaneous bumps can lose a count - acceptable for a blog view counter.

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

`items` omits `body` - the list never ships full article markdown. `total` is the
unpaged count so the site can render pagination without a second call.

### Document shape

```
id  content_type  title  slug  excerpt  body  author
cover_image_url  tags[]  views  created_date  updated_date
```

`content_type` and `views` are **not** caller-writable. The route owns the former;
`views` lives in object metadata and only changes through the bump endpoint.

`slug` is unique per content type, enforced on write. A post and a press release
may share a slug - they live under different prefixes and different site routes. A
collision returns `409`.

### Featured vendor (`content_type = featured_vendor`)

A third content type, for a home-page vendor spotlight. It has its own shape (no
slug/views):

```
id  content_type  vendor_id  subject  body
media[]{type: image|video, url}  starts  ends  created_date  updated_date
```

`vendor_id` is a reference into the main API's vendor table (this service does not
join it). `starts` defaults to now on create; `ends` is optional (open-ended).
`GET /featured` returns the single newest feature whose window contains "now"
(`starts <= now <= ends`, or no `ends`); it returns `{items: [], count: 0}` when
none is active, which the app treats as "fall back to a recently-added vendor"
(see LB-2.4).

## Layout

```
src/router.py    route map + dispatch; owns path-param extraction
src/storage.py   S3 client and object operations (keys, put/get/list, view bump)
src/posts.py     post + press-release handlers, shared by both content types
src/featured.py  featured-vendor handlers
src/health.py    liveness
src/utils.py     validation, serialization, responses, shared constants
config/*.yml     per-stage env vars, same style as the other sls services
tests/           moto-backed handler, storage, and router tests
```

## Local development

```bash
npm install
python3 -m venv .venv && .venv/bin/pip install -r requirements.development.txt
cp .env.example .env    # set BUCKET_NAME; the shell also needs AWS credentials
./serve.sh              # sls offline --noAuth on :5600
```

`sls offline` talks to the **real** S3 bucket, so the shell needs AWS credentials
(via `aws configure`, `AWS_PROFILE`, or access keys) with read/write access to the
bucket named in `BUCKET_NAME`. `--noAuth` skips API-key enforcement locally, so
every route is reachable without a key while developing.

```bash
curl localhost:5600/local/posts
curl -X POST localhost:5600/local/posts \
  -H 'content-type: application/json' \
  -d '{"title":"Why local wins","slug":"why-local-wins","body":"...","author":"Scott D"}'
# then bump its view count the way the site does after a page load:
curl -X POST localhost:5600/local/posts/<id>/views
```

## Tests

```bash
.venv/bin/python -m pytest
```

Tests run against `moto`, an in-process S3 mock - no network, no real bucket. The
slug-uniqueness `409` path and the metadata-only view bump are exercised against
the mock rather than assumed.

## Configuration

`config/<stage>.yml` holds the env vars, matching the style used by the other sls
services. Stages: `local`, `development`, `production`.

| Var              | Purpose                                                 |
|------------------|---------------------------------------------------------|
| `BUCKET_NAME`    | The S3 bucket holding the blog JSON objects             |
| `ALLOWED_ORIGIN` | CORS origin; `*` outside production                     |

The bucket is defined as a per-stage CloudFormation resource in `serverless.yml`
(`lytebuy-blog-<env>-<region>`) with public access blocked - the Lambda role is the
only reader/writer. It is intentionally **not versioned**: the view-bump copies
each object onto itself, which would otherwise accumulate a version per page view.

The Lambda's IAM role grants `s3:GetObject`/`PutObject`/`DeleteObject` on the
objects and `s3:ListBucket` on the bucket (also used by the health check's
`head_bucket`). `PutObject` covers `copy_object` onto self for the view bump.

## Deploy

```bash
sls deploy --stage development
sls deploy --stage production
```

A CloudFormation-managed bucket must be empty to delete and will fail to create if
the (globally unique) name is already taken.

## Migrating existing data

`scripts/migrate_mongo_to_s3.py` is a one-time backfill: it reads every document
from the old MongoDB collection and writes it to S3 as `{prefix}/{uuid}.json`,
setting `x-amz-meta-view-count` from the document's `views`. Run it against
development first, verify the counts, then production, before cutting the site and
admin over to the deployed S3-backed service. See the script's header for usage.
