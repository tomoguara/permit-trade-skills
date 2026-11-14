"""CLI commands for seeds and setup operations."""

import typer
from permit_trade_skills.config import db_url_option
from typing import Optional
from pathlib import Path

from permit_trade_skills.discovery.schemas import Jurisdiction, PlatformType
from permit_trade_skills.discovery import DiscoveryEngine
from permit_trade_skills.discovery.registry import Registry


app = typer.Typer(
    name="seeds",
    help="Seed jurisdiction management commands",
)


# Stage 05 detail extraction (docs/detail.md). The portal is Accela Citizen
# Access, server-rendered: LinkToRecord.aspx 302s twice to Cap/CapDetail.aspx on
# the same host, so plain HTTP (redirect_chase) gets the whole record; no
# browser needed. Stored under jurisdictions.detail_strategy.detail.
SEATTLE_DETAIL = {
    "fetch_strategy": "redirect_chase_then_soda",
    "allowed_hosts": ["services.seattle.gov"],
    "soda_resource_url": "https://cos-data.seattle.gov/resource/76t5-zqzr.json",
    "soda_key_column": "permitnum",
    "min_signals": 4,
    "rate_limits_per_minute": {
        "services.seattle.gov": 5,
        "cos-data.seattle.gov": 20,
    },
    # content_text catalog lines, in this order; other columns follow A-Z.
    "field_order": [
        ["PERMIT", "permitnum"],
        ["STATUS", "statuscurrent"],
        ["ADDRESS", "originaladdress1"],
        ["CITY", "originalcity"],
        ["STATE", "originalstate"],
        ["ZIP", "originalzip"],
        ["ISSUED", "issueddate"],
        ["APPLIED", "applieddate"],
        ["EXPIRES", "expiresdate"],
        ["COMPLETED", "completeddate"],
        ["DESCRIPTION", "description"],
        ["CLASSIFICATION", "permitclass"],
        ["CLASSIFICATION_MAPPED", "permitclassmapped"],
        ["PERMIT_TYPE", "permittypemapped"],
        ["PERMIT_TYPE_DESC", "permittypedesc"],
        ["EST_PROJECT_COST", "estprojectcost"],
        ["CONTRACTOR", "contractorcompanyname"],
        ["DETAIL_URL", "link"],
    ],
    "portal": {
        # Accela record header + the Record Details tab.
        "include": [".rec-left", "#tab-record_detail"],
        "field_labels": {
            "Record Number": "permit_number",
            "Record Status": "status",
            "Work Location": "address",
            "Project Description": "description",
            "Permit Issued Date": "issued_date",
            "Application Completed Date": "applied_date",
            "Expiration Date": "expires_date",
            "Total Issued Value": "valuation",
            "Choose the Primary Property Use": "classification",
            "Action Type": "scope",
        },
    },
}


# Canonical name -> Seattle source column (see the seed below for caveats).
SEATTLE_FIELD_MAP = {
    "permit_num": "permitnum",
    "permit_class": "permitclass",
    "permit_class_mapped": "permitclassmapped",
    "permit_type_mapped": "permittypemapped",
    "permit_type_desc": "permittypedesc",
    "status_current": "statuscurrent",
    "description": "description",
    "address": "originaladdress1",
    "city": "originalcity",
    "state": "originalstate",
    "zip_code": "originalzip",
    "housing_units": "housingunits",
    "latitude": "latitude",
    "longitude": "longitude",
    "issue_date": "issueddate",
    "valuation": "estprojectcost",
    "work_class": "workclass",
    "building_count": "numbuildings",
    "applied_at": "applieddate",
    "expires_at": "expiresdate",
    "completed_at": "completeddate",
    "housing_units_added": "housingunitsadded",
    "housing_units_removed": "housingunitsremoved",
    "housing_category": "housingcategory",
    "contractor_name": "contractorcompanyname",
}

# Canonical status (lower-case) -> raw `statuscurrent` values (case-insensitive).
SEATTLE_STATUS_MAP = {
    "pending": [
        "Initiated", "Pending", "Ready for Intake", "Scheduled",
        "Scheduled and Submitted", "Application Completed",
        "Reviews In Process", "Reviews Completed",
        "Additional Info Requested", "Awaiting Information",
        "Corrections Required", "Corrections Submitted",
        "Ready for Issuance",
    ],
    "issued": ["Issued", "Phase Issued", "Active"],
    "completed": [
        "Completed", "Closed", "Inspections Completed",
        "Approved to Occupy",
    ],
    "canceled": ["Canceled", "Withdrawn", "Denied"],
    "expired": ["Expired"],
}


@app.command(name="seattle")
def seattle_seed(
    db_url: str = db_url_option(),
    output_config: Optional[str] = typer.Option(None, "--output-config", "-o",
                                                help="Also export the registered row as YAML to this path"),
    check_columns: bool = typer.Option(False, "--check-columns",
                                       help="Check the mapped columns against the live columns.json "
                                            "(the Seattle API must be reachable)"),
) -> None:
    """
    Create seed jurisdiction for Seattle King County, WA.

    Re-running it updates the row in place (config_version + 1); the replaced
    config is kept in jurisdiction_versions (`pts config history`).
    """
    def run():
        # Create Seattle jurisdiction
        jurisdiction = Jurisdiction(
            id="seattle-kingcounty-wa",
            name="Seattle King County",
            state="WA",
            county="King County",
            platform_type=PlatformType.SOCRATA,
            base_url="https://cos-data.seattle.gov",
            api_endpoint="https://cos-data.seattle.gov/api/views",
            api_key=None,
            dataset_id="76t5-zqzr",
            dataset_name="Building Permits",
            config_version=1,
            # Canonical name -> source column. Matches the columns of the
            # downloaded export (data/wa-seattle-building-permits.json), which
            # is the only working source while the live API is down. Socrata
            # omits null fields per row, so check key coverage across all rows,
            # not just the first. work_class and building_count have no column
            # in the export; their source names are unverified guesses at the
            # API's names (NULL for file syncs).
            field_map=SEATTLE_FIELD_MAP,
            # Keys must be canonical Status values (lower-cased); values are
            # the raw `statuscurrent` strings in the Seattle data, matched
            # case-insensitively. Anything not listed maps to UNKNOWN.
            status_map=SEATTLE_STATUS_MAP,
            detail_strategy={
                "type": "dataset_api",
                "endpoint": "/api/views/{dataset_id}",
                "method": "GET",
                "pagination": {
                    "type": "offset",
                    "offset_param": "$offset",
                    "limit_param": "$limit",
                    "max_limit": 1000,
                },
                "filter_param": "$where",
                # Incremental-sync cursor column: `$where=issueddate > <cursor>`.
                "timestamp_field": "issueddate",
                # Detail URL for rows without a `link` ({{column}} = raw row value).
                "detail_url_rule": (
                    "https://services.seattle.gov/portal/customize/"
                    "LinkToRecord.aspx?altId={{permitnum}}"
                ),
                # Stage 05 detail extraction (docs/detail.md)
                "detail": SEATTLE_DETAIL,
            },
            is_active=True,
        )

        from permit_trade_skills.cli.discover import check_config, print_validation

        validation = check_config(jurisdiction.field_map, jurisdiction.status_map,
                                  jurisdiction.detail_strategy, jurisdiction.base_url,
                                  jurisdiction.dataset_id, check_columns)
        print_validation(validation)
        if not validation.ok:
            typer.echo("Error: the seed config has validation errors; nothing registered.", err=True)
            raise typer.Exit(code=1)

        engine = DiscoveryEngine(db_url=db_url)

        # Create tables if needed
        from permit_trade_skills.discovery.registry import Registry
        registry = Registry(database_url=db_url)
        registry.create_tables()

        # Register jurisdiction
        session = registry.get_session()
        try:
            db_jurisdiction = registry.add_jurisdiction(session, jurisdiction,
                                                        replaced_by="pts seeds seattle")
            typer.echo(f"Jurisdiction registered: {db_jurisdiction.id}")
            typer.echo(f"Config version: {db_jurisdiction.config_version}")
        finally:
            session.close()

        # Export the registered row (with its real config_version) if requested
        if output_config:
            config_path = Path(output_config)
            engine.export_config(jurisdiction.id, config_path)
            typer.echo(f"Config exported to: {config_path}")

    run()


if __name__ == "__main__":
    app()
