""" Connection-string templating """
import pytest

from src.utils import resolve_uri

BASE = "mongodb+srv://u:p@cluster0-xkkdg.mongodb.net/{}?retryWrites=true&w=majority"


def test_round_bracket_placeholder_is_substituted():
    uri = resolve_uri(BASE.format("((db_name))"), "lytebuy-development")
    assert uri == BASE.format("lytebuy-development")
    assert "((db_name))" not in uri


def test_a_uri_without_a_placeholder_is_untouched():
    plain = BASE.format("lytebuy-production")
    assert resolve_uri(plain, "lytebuy-development") == plain


def test_every_occurrence_is_replaced():
    uri = resolve_uri(
        "mongodb+srv://u:p@host/((db_name))?appName=((db_name))", "lytebuy-test"
    )
    assert uri == "mongodb+srv://u:p@host/lytebuy-test?appName=lytebuy-test"


@pytest.mark.parametrize("empty", [None, ""])
def test_a_missing_uri_passes_through(empty):
    # get_client turns this into an explicit "DB_URL is not set" error; the
    # substitution step must not raise first.
    assert resolve_uri(empty, "lytebuy-development") == empty


def test_the_placeholder_survives_the_ampersands_in_a_real_uri():
    uri = resolve_uri(BASE.format("((db_name))"), "lytebuy-production")
    assert uri.endswith("?retryWrites=true&w=majority")
    assert "/lytebuy-production?" in uri
