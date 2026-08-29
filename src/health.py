""" Liveness check """
import os

from botocore.exceptions import BotoCoreError, ClientError

from src.storage import BUCKET, get_s3_client
from src.utils import response


def health(_event, _context):
    """ GET /health - public

    head_bucket the storage bucket so an uptime monitor sees a red service when
    the backend is unreachable, rather than a green one that 503s on every real
    request. The body key stays named "database" so existing monitors keep working.
    """
    database = "ok"
    try:
        get_s3_client().head_bucket(Bucket=BUCKET)
    except (ClientError, BotoCoreError, RuntimeError) as err:
        database = f"unavailable: {err}"

    return response(200 if database == "ok" else 503, {
        "service": "lytebuy-blog-service",
        "env": os.getenv("ENV", "unknown"),
        "database": database,
    })
