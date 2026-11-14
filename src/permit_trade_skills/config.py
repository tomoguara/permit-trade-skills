"""Environment configuration: loads variables from a project .env file."""

import os
from typing import Optional

from dotenv import find_dotenv, load_dotenv

_loaded = False


def load_env() -> None:
    """Load the nearest .env (searching up from the working directory) once.

    Variables already set in the process environment take precedence over
    .env values, so `HF_TOKEN=... pts ...` still overrides the file.
    """
    global _loaded
    if _loaded:
        return
    load_dotenv(find_dotenv(usecwd=True), override=False)
    _loaded = True


def get_hf_token() -> Optional[str]:
    """Hugging Face Hub token from HF_TOKEN, or None if unset/blank."""
    load_env()
    return os.environ.get("HF_TOKEN") or None


DB_URL_ENVVAR = "PERMIT_TRADE_SKILLS_DB_URL"
DEFAULT_DB_URL = "postgresql://postgres:postgres@localhost:5432/permit_trade_skills"


def get_db_url() -> str:
    """Database URL from PERMIT_TRADE_SKILLS_DB_URL (environment or .env), else the local default."""
    load_env()
    return os.environ.get(DB_URL_ENVVAR) or DEFAULT_DB_URL


def db_url_option():
    """Shared typer option for --db-url.

    Precedence: --db-url flag > PERMIT_TRADE_SKILLS_DB_URL (shell or .env) > DEFAULT_DB_URL.
    The default is resolved lazily so .env is loaded first and --help doesn't
    print a URL that may contain a password.
    """
    import typer

    return typer.Option(
        None,
        "--db-url",
        help=f"Database URL. Defaults to ${DB_URL_ENVVAR} (shell or .env), else the local Docker DB.",
        callback=lambda value: value or get_db_url(),
        show_default=False,
    )
