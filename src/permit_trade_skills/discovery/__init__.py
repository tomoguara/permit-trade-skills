"""Main discovery module for catalog discovery and jurisdiction registration."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from pathlib import Path

from permit_trade_skills.discovery.catalog_client import CatalogClient
from permit_trade_skills.discovery.verifier import Verifier
from permit_trade_skills.discovery.suggest import Scorer
from permit_trade_skills.discovery.schemas import (
    DiscoveryCandidate,
    DiscoveryDossier,
    DiscoveryRun,
    DiscoveryStatus,
    Jurisdiction,
    PlatformType,
)
from permit_trade_skills.discovery.registry import Registry


from permit_trade_skills.config import get_db_url


class JurisdictionExistsError(ValueError):
    """Registering would overwrite an existing jurisdiction (pass replace=True)."""


class DiscoveryEngine:
    """Main discovery engine coordinating all components."""

    def __init__(
        self,
        catalog_url: str = "https://catalog.data.gov/api/3/action",
        db_url: Optional[str] = None,
        transport=None,
    ):
        # transport: optional httpx transport for the catalog client and the
        # verifier (tests inject httpx.MockTransport).
        self._transport = transport
        self.catalog_client = CatalogClient(base_url=catalog_url, transport=transport)
        # None -> PERMIT_TRADE_SKILLS_DB_URL (shell or .env), else the local default
        self.registry = Registry(database_url=db_url or get_db_url())
        self.scorer: Optional[Scorer] = None
        self.verifier: Optional[Verifier] = None

    def _record_run(self, run: DiscoveryRun, dossier: DiscoveryDossier,
                    evidence_dir: Optional[Path]) -> Optional[Path]:
        """Save the run to discovery_runs (unlinked until register) and mirror the
        dossier to `<evidence_dir>/<jurisdiction_id>/discovery/<date>-<run>.json`.

        The evidence copy leaves out each candidate's raw catalog `metadata`
        (the full CKAN package); everything else is kept. Returns its path.
        """
        session = self.registry.get_session()
        try:
            dossier.discovery_run_id = self.registry.save_discovery_run(session, run)
        finally:
            session.close()
        if evidence_dir is None:
            return None
        evidence = dossier.model_dump(mode="json")
        for candidate in evidence["candidates"] + ([evidence["selected_candidate"]]
                                                  if evidence.get("selected_candidate") else []):
            candidate.pop("metadata", None)
        evidence["discovery_run"] = run.model_dump(mode="json") | {"id": dossier.discovery_run_id}
        stamp = (run.started_at or datetime.utcnow()).strftime("%Y-%m-%d")
        path = (Path(evidence_dir) / dossier.jurisdiction_id / "discovery"
                / f"{stamp}-{dossier.discovery_run_id[:8]}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2, default=str))
        return path

    async def discover_from_local_file(
        self,
        permit_data: list,
        jurisdiction_name: str,
        state: str,
        output_path: Path = None,
        jurisdiction_id: str = None,
        dataset_id: str = None,
        platform_type: PlatformType = PlatformType.SOCRATA,
        source_path: Optional[str] = None,
        evidence_dir: Optional[Path] = None,
    ) -> DiscoveryDossier:
        """
        Discover datasets from a local JSON file instead of catalog.data.gov.
        
        This is useful when catalog.data.gov API is unavailable.
        
        Args:
            permit_data: List of permit records from local JSON file
            jurisdiction_name: Name of the jurisdiction (e.g., "Seattle")
            state: State abbreviation (e.g., "WA")
            output_path: Optional path to save the dossier
            jurisdiction_id: Optional jurisdiction ID (auto-generated if not provided)
            dataset_id: Optional dataset ID (e.g., "76t5-zqzr" for Seattle)
            platform_type: Platform type (default: SOCRATA)
            source_path: The file the rows came from (recorded on the run)
            evidence_dir: Where to mirror the dossier (None: no copy)
        
        Returns:
            DiscoveryDossier with candidates (from local data)
        """
        
        # Ensure tables exist
        self.registry.create_tables()
        started_at = datetime.utcnow()
        
        # Generate jurisdiction_id if not provided
        if jurisdiction_id is None:
            jurisdiction_id = f"{jurisdiction_name.lower().replace(' ', '-')}-{state.lower()}"
        
        # Create the dossier with local data as the single candidate
        dossier = DiscoveryDossier(
            jurisdiction_id=jurisdiction_id,
            jurisdiction_name=jurisdiction_name,
            candidates=[],
            verification_results=[],
            created_at=None,
        )
        
        # Create a DiscoveryCandidate from the local file info
        # Use a placeholder URL since we have local data
        dataset_name = "Building Permits" if not dataset_id else f"Dataset {dataset_id}"
        candidate = DiscoveryCandidate(
            id=dataset_id or "local-data",
            title=f"{jurisdiction_name} - {dataset_name}",
            organization=jurisdiction_name,
            url=f"https://local-data://{jurisdiction_id}",
            platform_type=platform_type,
            platform_detected_by="local_file",
            confidence=1.0,  # Trust local data fully
            metadata={
                "source": "local_file",
                "jurisdiction": jurisdiction_name,
                "state": state,
                "dataset_id": dataset_id,
                "record_count": len(permit_data),
            },
            evidence={
                "file_path": str(output_path) if output_path else "in-memory",
                "records_loaded": len(permit_data),
            },
        )
        dossier.candidates.append(candidate)
        
        # No verification needed for local data
        # But add a note about the source
        dossier.verification_results = []
        dossier.selected_candidate = candidate
        dossier.notes = f"Discovered from local file. {len(permit_data)} records loaded."
        
        # Guess the field and status maps (review them before syncing).
        from permit_trade_skills.discovery.mapping import (
            guess_field_map, guess_status_map, sample_columns,
        )

        field_map = guess_field_map(sample_columns(permit_data))
        dossier.field_mapping = field_map
        status_column = field_map.get("status_current")
        if status_column:
            status_values = [r.get(status_column) for r in permit_data if isinstance(r, dict)]
            status_map, unmatched = guess_status_map(status_values)
            dossier.status_mapping = status_map
            if unmatched:
                dossier.notes += (
                    f" Unmapped status values (sync as UNKNOWN until added to status_map): "
                    f"{', '.join(unmatched)}."
                )
        else:
            dossier.notes += " No status column recognised; status_mapping is empty."

        run = DiscoveryRun(
            search_query="local-file",
            search_parameters={"jurisdiction": jurisdiction_name, "state": state,
                               "jurisdiction_id": jurisdiction_id, "source": "local_file",
                               "path": source_path},
            total_results=len(permit_data), candidates_found=1,
            status=DiscoveryStatus.COMPLETED, started_at=started_at, completed_at=datetime.utcnow(),
        )
        self._record_run(run, dossier, evidence_dir)

        if output_path:
            self.catalog_client.save_dossier(dossier, output_path)
        
        return dossier

    async def discover(
        self,
        jurisdiction_name: str,
        state: str,
        output_path: Path = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
        evidence_dir: Optional[Path] = None,
    ) -> DiscoveryDossier:
        """
        Discover datasets for a jurisdiction.

        Args:
            jurisdiction_name: Name of the jurisdiction (e.g., "Seattle")
            state: State abbreviation (e.g., "WA")
            output_path: Optional path to save the dossier
            query: Custom catalog search instead of the default name queries
            limit: Maximum candidates (default CatalogClient.MAX_RESULTS)
            evidence_dir: Where to mirror the dossier (None: no copy)

        Returns:
            DiscoveryDossier with candidates and verification results
        """
        # Ensure tables exist
        self.registry.create_tables()
        
        dossier = DiscoveryDossier(
            jurisdiction_id=f"{jurisdiction_name.lower().replace(' ', '-')}-{state.lower()}",
            jurisdiction_name=jurisdiction_name,
            candidates=[],
            verification_results=[],
            created_at=None,
        )

        async with self.catalog_client as client:
            run, candidates = await client.discovery_run(
                search_query=query or jurisdiction_name,
                jurisdiction_name=jurisdiction_name,
                state=state,
                query=query,
                limit=limit,
            )
        run.search_parameters["jurisdiction_id"] = dossier.jurisdiction_id

        dossier.candidates = candidates

        # Score candidates
        self.scorer = Scorer(jurisdiction_name, state)
        self.scorer.score_and_rank(dossier.candidates)

        # Verify top candidates
        async with Verifier(transport=self._transport) as verifier:
            for candidate in dossier.candidates[:5]:  # Verify top 5
                probe_result = await verifier.verify(candidate.url)
                dossier.verification_results.append(probe_result)

                # Update candidate with verification info
                if probe_result.detected_platform:
                    candidate.platform_type = probe_result.detected_platform
                    candidate.platform_detected_by = probe_result.probe_name
                    candidate.confidence *= probe_result.confidence or 0.5

        # Re-score with verification info
        self.scorer.score_and_rank(dossier.candidates)

        # Select best candidate
        if dossier.candidates:
            dossier.selected_candidate = dossier.candidates[0]
            dossier.notes = f"Selected candidate: {dossier.selected_candidate.title}"
        elif run.error_message:
            dossier.notes = f"Catalog search failed: {run.error_message}"

        self._record_run(run, dossier, evidence_dir)

        # Save dossier if output path provided
        if output_path:
            self.catalog_client.save_dossier(dossier, output_path)

        return dossier

    async def register(
        self,
        dossier: DiscoveryDossier,
        jurisdiction: Jurisdiction,
        field_map: Dict[str, Any] = None,
        status_map: Dict[str, Any] = None,
        detail_strategy: Dict[str, Any] = None,
        replace: bool = False,
        replaced_by: Optional[str] = "pts discovery register",
    ) -> str:
        """
        Register a jurisdiction in the database.

        Args:
            dossier: The discovery dossier
            jurisdiction: The jurisdiction configuration
            field_map: Optional field mapping
            status_map: Optional status mapping
            detail_strategy: Optional detail fetching strategy
            replace: Overwrite an existing jurisdiction with the same id
                (its config_version is incremented). Without it, an existing
                id raises JurisdictionExistsError and nothing is written.
            replaced_by: Recorded in jurisdiction_versions when replacing.

        Returns:
            The jurisdiction ID
        """
        if not replace:
            session = self.registry.get_session()
            try:
                exists = self.registry.jurisdiction_exists(session, jurisdiction.id)
            finally:
                session.close()
            if exists:
                raise JurisdictionExistsError(
                    f"Jurisdiction '{jurisdiction.id}' is already registered; registering it again "
                    f"would replace its field map, status map and detail strategy. "
                    f"Pass --replace to do that."
                )

        # Apply mappings from dossier if not provided
        if field_map:
            jurisdiction.field_map = field_map
        if status_map:
            jurisdiction.status_map = status_map
        if detail_strategy:
            jurisdiction.detail_strategy = detail_strategy

        # No platform on the selected candidate: take the first probe that detected one.
        candidate = dossier.selected_candidate
        if dossier.verification_results and not (candidate and candidate.platform_type):
            for result in dossier.verification_results:
                if result.detected_platform:
                    jurisdiction.platform_type = result.detected_platform
                    break

        if dossier.selected_candidate:
            jurisdiction.api_endpoint = dossier.selected_candidate.url
            jurisdiction.dataset_id = dossier.selected_candidate.id
            jurisdiction.dataset_name = dossier.selected_candidate.title

        # Save to registry
        session = self.registry.get_session()
        try:
            self.registry.add_jurisdiction(session, jurisdiction, replaced_by=replaced_by)
            self.registry.save_dossier(session, dossier)
            if dossier.discovery_run_id:
                self.registry.link_discovery_run(session, dossier.discovery_run_id, jurisdiction.id)
        finally:
            session.close()

        return jurisdiction.id

    @staticmethod
    def jurisdiction_from_dossier(
        dossier: DiscoveryDossier,
        name: str,
        state: str,
        county: Optional[str] = None,
    ) -> Jurisdiction:
        """The Jurisdiction a dossier registers: its selected candidate plus the
        dossier's field map, status map and detail strategy."""
        candidate = dossier.selected_candidate
        platform = candidate.platform_type if candidate else None
        if platform is None:  # same fallback as register(): first probe that detected one
            platform = next((r.detected_platform for r in dossier.verification_results
                             if r.detected_platform), None)
        return Jurisdiction(
            id=dossier.jurisdiction_id,
            name=name,
            state=state,
            county=county,
            platform_type=platform or PlatformType.OTHER,
            base_url="",
            api_endpoint=candidate.url if candidate else None,
            dataset_id=candidate.id if candidate else None,
            dataset_name=candidate.title if candidate else None,
            config_version=1,
            field_map=dossier.field_mapping or {},
            status_map=dossier.status_mapping or {},
            detail_strategy=dossier.detail_strategy or {},
            is_active=True,
        )

    def export_config(
        self,
        jurisdiction_id: str,
        output_path: Path,
    ) -> bool:
        """Export jurisdiction configuration to YAML."""
        import yaml

        session = self.registry.get_session()
        try:
            config = self.registry.export_config(session, jurisdiction_id)
            if not config:
                return False

            # Clean up for export
            config.pop("id", None)  # Don't export internal ID
            config["exported_at"] = config.pop("updated_at")

            output_path.parent.mkdir(parents=True, exist_ok=True)

            with open(output_path, "w") as f:
                yaml.dump(config, f, default_flow_style=False, sort_keys=False)

            return True

        finally:
            session.close()


async def discover_jurisdiction(
    jurisdiction_name: str,
    state: str,
    db_url: Optional[str] = None,
    output_path: Path = None,
) -> DiscoveryDossier:
    """
    Convenience function to discover a jurisdiction.

    Args:
        jurisdiction_name: Name of the jurisdiction
        state: State abbreviation
        db_url: Database URL
        output_path: Optional path to save the dossier

    Returns:
        DiscoveryDossier
    """
    engine = DiscoveryEngine(db_url=db_url)
    return await engine.discover(jurisdiction_name, state, output_path)


async def register_jurisdiction(
    dossier: DiscoveryDossier,
    jurisdiction: Jurisdiction,
    db_url: Optional[str] = None,
    replace: bool = False,
) -> str:
    """
    Convenience function to register a jurisdiction.

    Args:
        dossier: The discovery dossier
        jurisdiction: The jurisdiction configuration
        db_url: Database URL

    Returns:
        The jurisdiction ID
    """
    engine = DiscoveryEngine(db_url=db_url)
    return await engine.register(dossier, jurisdiction, replace=replace)
