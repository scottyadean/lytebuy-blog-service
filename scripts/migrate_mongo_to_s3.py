""" One-time backfill: copy the blog records from MongoDB into S3.

Reads every document from the old `posts` collection and writes it to S3 in the
shape the S3-backed service expects: one JSON object at {prefix}/{uuid}.json with
a fresh uuid, dates as iso strings, and the view counter moved into object
metadata (x-amz-meta-view-count) rather than the body.

This is NOT part of the Lambda. Run it once per environment, against development
first, before cutting the site and admin over to the deployed service.

Usage:
    # needs pymongo installed and AWS credentials with write access to the bucket
    pip install 'pymongo>=4.7' dnspython
    export DB_URL='mongodb+srv://user:pass@cluster.mongodb.net/lytebuy-development?...'
    export BUCKET_NAME='lytebuy-blog-development-us-west-2'
    python scripts/migrate_mongo_to_s3.py            # write for real
    python scripts/migrate_mongo_to_s3.py --dry-run  # count only, no writes

Idempotency: each run mints fresh uuids, so running it twice DUPLICATES every
record. Empty the prefixes (or the bucket) before re-running.
"""
import argparse
import os
import sys

# Run from the repo root so `src` is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient  # noqa: E402  (import after sys.path tweak)

from src import storage  # noqa: E402
from src.utils import PREFIXES, format_timestamp  # noqa: E402

COLLECTION = "posts"

# Fields the S3 body must not carry: the id becomes the object key, and the view
# count moves to object metadata.
_BODY_EXCLUDED = ("_id", "views")

# Date fields to render from BSON datetimes to iso strings, per content type.
_DATE_FIELDS = ("created_date", "updated_date", "starts", "ends")


def _to_body(document):
    """ turn a mongo document into the JSON body S3 stores (named transform) """
    body = {
        key: value
        for key, value in document.items()
        if key not in _BODY_EXCLUDED
    }
    for field in _DATE_FIELDS:
        if field in body:
            body[field] = format_timestamp(body[field])
    return body


def _view_count(document):
    """ the starting view count for a migrated record (named helper) """
    return int(document.get("views") or 0)


def migrate(dry_run=False):
    """ copy every document from mongo into s3, returns a per-type count """
    uri = os.environ["DB_URL"]
    client = MongoClient(uri)
    # The database name is carried in the uri path.
    collection = client.get_default_database()[COLLECTION]

    counts = {content_type: 0 for content_type in PREFIXES}
    for document in collection.find({}):
        content_type = document.get("content_type")
        if content_type not in PREFIXES:
            print(f"skip: unknown content_type {content_type!r} on {document.get('_id')}")
            continue

        body = _to_body(document)
        views = _view_count(document)
        item_id = storage.new_id()

        if not dry_run:
            storage.put_object(content_type, item_id, body, view_count=views)
        counts[content_type] += 1

    return counts


def main():
    parser = argparse.ArgumentParser(description="Backfill blog records from MongoDB to S3.")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    args = parser.parse_args()

    if not os.environ.get("BUCKET_NAME"):
        parser.error("BUCKET_NAME must be set")
    if not os.environ.get("DB_URL"):
        parser.error("DB_URL must be set")

    counts = migrate(dry_run=args.dry_run)
    verb = "would migrate" if args.dry_run else "migrated"
    for content_type, count in counts.items():
        print(f"{verb} {count} {content_type} -> {PREFIXES[content_type]}")
    print(f"{verb} {sum(counts.values())} records total")


if __name__ == "__main__":
    main()
