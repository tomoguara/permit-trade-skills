"""Main entry point for permit-trade-skills."""

import typer

from permit_trade_skills.cli.discover import cli as discover_cli
from permit_trade_skills.cli.register import cli as register_cli
from permit_trade_skills.cli.config import cli as config_cli
from permit_trade_skills.cli.seeds import app as seeds_app
from permit_trade_skills.cli.sync import cli as sync_cli
from permit_trade_skills.cli.search import cli as search_cli
from permit_trade_skills.cli.db.commands import cli as db_cli
from permit_trade_skills.cli.query import api_cli, query_command
from permit_trade_skills.cli.detail import cli as detail_cli
from permit_trade_skills.cli.skills import cli as skills_cli
from permit_trade_skills.cli.permit import cli as permit_cli
from permit_trade_skills.cli.agent import cli as agent_cli
from permit_trade_skills.cli.pipeline import cli as pipeline_cli
from permit_trade_skills.cli.review import cli as review_cli
from permit_trade_skills.cli.eval import cli as eval_cli
from permit_trade_skills.cli.label import cli as label_cli
from permit_trade_skills.cli.quality import cli as quality_cli
from permit_trade_skills.cli.alerts import cli as alerts_cli

app = typer.Typer(
    name="permit-trade-skills",
    help="Building permit tracking and jurisdiction discovery system",
)

app.add_typer(discover_cli)
app.add_typer(register_cli)
app.add_typer(config_cli)
app.add_typer(seeds_app)
app.add_typer(sync_cli)
app.add_typer(search_cli)
app.add_typer(db_cli)
app.add_typer(api_cli)
app.add_typer(detail_cli)
app.add_typer(skills_cli)
app.add_typer(permit_cli)
app.add_typer(agent_cli)
app.add_typer(pipeline_cli)
app.add_typer(review_cli)
app.add_typer(eval_cli)
app.add_typer(label_cli)
app.add_typer(quality_cli)
app.add_typer(alerts_cli)
app.command(name="query")(query_command)


def main() -> None:
    from permit_trade_skills.config import load_env

    load_env()
    app()


if __name__ == "__main__":
    main()
