"""Register CLI commands."""

import typer

from permit_trade_skills.cli.discover import register_command

cli = typer.Typer(
    name="register",
    help="Register discovered jurisdictions",
)

# Same command as `pts discovery register` (same options and code path).
cli.command(
    name="jurisdiction",
    help="Register a discovered jurisdiction in the database (same as `pts discovery register`).",
)(register_command)
