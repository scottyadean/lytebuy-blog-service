# Writing blog posts with an AI agent

This guide is written for an AI coding agent (Claude Code or similar) that has
been asked to draft and publish a Lytebuy blog post or press release directly,
with no UI. It talks to the blog service over HTTP. Read it fully before making any write call.

## What you are writing into

One MongoDB collection holds both **posts** and **press releases**. They share
an identical shape; the only difference is the route you POST to. The site
renders the `body` field as **Markdown**, sanitized server-side, so write the
body as Markdown.

## Base URL and auth

- Base URL comes from the operator (env `BLOG_API_URL`). Locally it is
  `http://localhost:5600/local` when `sls offline` is running.
- Every write and single-item read needs the API key in an `x-api-key` header.
  The key is env `BLOG_API_KEY`. **Never print the key or commit it.** Read it
  from the environment at call time.
- List endpoints (`GET /posts`, `GET /press-releases`) are public and need no key.

If the operator has not given you a base URL and key, stop and ask for them.

## Endpoints you will use

| Goal | Method | Path |
|------|--------|------|
| See what already exists (and current slugs) | GET | `/posts` or `/press-releases` |
| Read one in full | GET | `/posts/{id}` |
| Publish a new one | POST | `/posts` or `/press-releases` |
| Revise an existing one | PUT | `/posts/{id}` or `/press-releases/{id}` |

There is **no delete endpoint.** Do not try to delete; if the operator wants a
post removed, tell them it must be done in the service or database directly.

## The document you send

POST body (all writes use this JSON shape):

```json
{
  "title": "Why local artisans win",
  "slug": "why-local-artisans-win",
  "excerpt": "A short summary shown in listings and meta tags. Optional.",
  "body": "# Heading\n\nMarkdown body...",
  "author": "Scott Dean",
  "cover_image_url": "https://.../cover.jpg",
  "tags": ["artisan", "local"]
}
```

Field rules (the service rejects anything that breaks them):

| Field | Required | Rules |
|-------|----------|-------|
| `title` | yes | non-empty, <= 200 chars |
| `slug` | yes | lowercase `a-z0-9-` only, no leading/trailing/doubled hyphens, <= 220, **unique per content type** |
| `body` | yes | Markdown, non-empty, <= 100000 chars |
| `author` | yes | non-empty, <= 120 chars |
| `excerpt` | no | <= 400 chars, or omit / `null` |
| `cover_image_url` | no | <= 2000 chars, or omit / `null` |
| `tags` | no | up to 12 strings, each <= 40 chars |

`content_type`, `views`, `id`, `created_date`, `updated_date` are owned by the
service. Do not send them.

### Deriving a good slug

Lowercase the title, replace every run of non-alphanumerics with a single
hyphen, and trim leading/trailing hyphens. `"Why Local Artisans Win!"` becomes
`why-local-artisans-win`. Before POSTing, `GET /posts` and confirm no existing
item already uses that slug for the same content type. A duplicate returns
`409 slug is already in use`.

### Markdown the site will render

The site allows these tags after sanitizing: `h2 h3 h4 p a ul ol li blockquote
strong em code pre img hr br figure figcaption table thead tbody tr th td`.
Write with those in mind:

- Use `##` and below for headings; a single `#` (h1) is stripped. The page
  already renders the title as the h1.
- Links to `http`/`https`/`mailto` only. External links are auto-marked
  `nofollow` and open in a new tab.
- Images need a real `src`; `loading="lazy"` is added automatically.
- No raw HTML, `<script>`, `<style>`, or inline event handlers survive sanitizing.

## Recommended flow

1. `GET /posts` (or `/press-releases`) to see existing titles and slugs.
2. Draft the Markdown body. Propose the title, slug, excerpt and tags to the
   operator and get sign-off before publishing.
3. Validate against the rules above locally so you fail fast, not on the wire.
4. `POST` the JSON with the `x-api-key` header. On `201`, report back the new
   `id` and slug; the post is live at `https://<site>/blog/<slug>`.
5. To revise, `PUT /posts/{id}` with only the fields that change (a partial
   update never blanks a field you omit; sending `null` for an optional field
   clears it).

## curl examples

Create a post:

```bash
curl -sS -X POST "$BLOG_API_URL/posts" \
  -H "x-api-key: $BLOG_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "title": "Why local artisans win",
    "slug": "why-local-artisans-win",
    "excerpt": "Small makers, real stories, better margins.",
    "body": "## The case for local\n\nMarkdown goes here...",
    "author": "Scott Dean",
    "tags": ["artisan", "local"]
  }'
```

Revise the body of an existing post:

```bash
curl -sS -X PUT "$BLOG_API_URL/posts/<id>" \
  -H "x-api-key: $BLOG_API_KEY" \
  -H 'content-type: application/json' \
  -d '{ "body": "## Updated copy\n\n..." }'
```

## Errors you may see

| Status | Meaning | What to do |
|--------|---------|------------|
| `400` | validation failed | the JSON body includes an `error` message naming the field; fix and retry |
| `403` | missing / wrong API key | check `BLOG_API_KEY`; the deployed gateway rejects keyless writes |
| `404` | id not found | you are editing something that does not exist; re-list to find the right id |
| `409` | slug already in use | pick a different slug |
| `503` | database unreachable | transient; retry shortly, then tell the operator |

## Do not

- Do not print or log the API key.
- Do not invent `id`, `views`, or timestamps.
- Do not publish without the operator approving the final title, slug and body.
- Do not attempt deletes; the endpoint does not exist.
