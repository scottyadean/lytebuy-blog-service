""" Liveness check """
import os

from pymongo.errors import PyMongoError

from src.utils import get_client, response


def health(_event, _context):
    """ GET /health - public

    Pings mongo so an uptime monitor sees a red service when the database is
    unreachable, rather than a green one that 503s on every real request.
    """
    database = "ok"
    try:
        get_client().admin.command("ping")
    except (PyMongoError, RuntimeError) as err:
        database = f"unavailable: {err}"

    return response(200 if database == "ok" else 503, {
        "service": "lytebuy-blog-service",
        "env": os.getenv("ENV", "unknown"),
        "database": database,
    })
