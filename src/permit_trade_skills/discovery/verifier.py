"""Platform verification probes for detecting data platforms."""

import httpx
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from permit_trade_skills.discovery.schemas import PlatformType, VerificationResult, ProbeResult


class Verifier:
    """Verifies the platform type of a dataset URL."""

    def __init__(self, timeout: float = 10.0, max_retries: int = 2,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.timeout = timeout
        self.max_retries = max_retries
        self._transport = transport  # tests inject httpx.MockTransport
        self._client: Optional[httpx.AsyncClient] = None

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": "PermitTradeSkills/1.0"},
            transport=self._transport,
        )

    async def __aenter__(self):
        self._client = self._new_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    async def verify(self, url: str) -> ProbeResult:
        """
        Verify the platform type for a given URL.

        Args:
            url: The dataset URL to verify

        Returns:
            ProbeResult with platform detection information
        """
        probes = [
            self._probe_socrata,
            self._probe_ckan,
            self._probe_generic_api,
        ]

        for probe in probes:
            result = await probe(url)
            if result.success and result.detected_platform:
                return result

        # Default to other if no probe succeeded
        return ProbeResult(
            probe_name="fallback",
            success=True,
            detected_platform=PlatformType.OTHER,
            confidence=0.0,
            details={"reason": "No platform detected, assuming generic"},
        )

    async def _probe_socrata(self, url: str) -> ProbeResult:
        """Probe for Socrata platform."""
        try:
            parsed = urlparse(url)
            domain = parsed.netloc

            # Socrata domains typically follow this pattern
            if "soda" in domain or "socrata" in domain or domain.endswith(".gov"):
                if self._client is None:
                    self._client = self._new_client()

                # Try to fetch metadata endpoint
                test_urls = [
                    f"https://{domain}/api/views.json",
                    f"https://{domain}/api/views/4000.json",
                ]

                for test_url in test_urls:
                    try:
                        response = await self._client.get(test_url, timeout=5.0)
                        if response.status_code == 200:
                            data = response.json()
                            # Socrata responses contain specific keys
                            if isinstance(data, dict) and ("meta" in data or "view" in data):
                                return ProbeResult(
                                    probe_name="socrata_api_check",
                                    success=True,
                                    detected_platform=PlatformType.SOCRATA,
                                    confidence=0.95,
                                    details={
                                        "domain": domain,
                                        "api_endpoint": test_url,
                                        "response_keys": list(data.keys()),
                                    },
                                )
                    except (httpx.RequestError, ValueError):
                        continue

                # If we got here, try a simpler check
                return ProbeResult(
                    probe_name="socrata_domain_check",
                    success=True,
                    detected_platform=PlatformType.SOCRATA,
                    confidence=0.75,
                    details={
                        "domain": domain,
                        "reason": "Socrata-like domain pattern detected",
                    },
                )

        except Exception as e:
            return ProbeResult(
                probe_name="socrata",
                success=False,
                error_message=str(e),
            )

        return ProbeResult(
            probe_name="socrata",
            success=True,
            detected_platform=None,
            confidence=0.0,
            details={"reason": "No Socrata indicators found"},
        )

    async def _probe_ckan(self, url: str) -> ProbeResult:
        """Probe for CKAN platform."""
        try:
            if self._client is None:
                self._client = self._new_client()

            # Try CKAN API endpoint
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            ckan_endpoints = [
                f"{base_url}/api/3/action/package_list",
                f"{base_url}/api/3/action/status_show",
                f"{base_url}/api/rest/dataset",
            ]

            for endpoint in ckan_endpoints:
                try:
                    response = await self._client.get(endpoint, timeout=5.0)
                    if response.status_code == 200:
                        data = response.json()
                        # CKAN responses have specific structure
                        if isinstance(data, dict) and "result" in data:
                            result = data["result"]
                            if isinstance(result, (list, dict)):
                                return ProbeResult(
                                    probe_name="ckan_api_check",
                                    success=True,
                                    detected_platform=PlatformType.CKAN,
                                    confidence=0.90,
                                    details={
                                        "endpoint": endpoint,
                                        "response_type": type(result).__name__,
                                        "response_keys": list(result.keys()) if isinstance(result, dict) else f"list with {len(result)} items",
                                    },
                                )
                except (httpx.RequestError, ValueError):
                    continue

        except Exception as e:
            return ProbeResult(
                probe_name="ckan",
                success=False,
                error_message=str(e),
            )

        return ProbeResult(
            probe_name="ckan",
            success=True,
            detected_platform=None,
            confidence=0.0,
            details={"reason": "No CKAN indicators found"},
        )

    async def _probe_generic_api(self, url: str) -> ProbeResult:
        """Probe for generic API endpoints."""
        try:
            if self._client is None:
                self._client = self._new_client()

            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

            # Try common API endpoints
            api_endpoints = [
                f"{base_url}/api",
                f"{base_url}/api/v1",
                f"{base_url}/api/v1.0",
                f"{base_url}/data.json",
            ]

            for endpoint in api_endpoints:
                try:
                    response = await self._client.get(endpoint, timeout=5.0)
                    if response.status_code == 200:
                        return ProbeResult(
                            probe_name="generic_api_check",
                            success=True,
                            detected_platform=PlatformType.OTHER,
                            confidence=0.60,
                            details={
                                "endpoint": endpoint,
                                "status_code": response.status_code,
                            },
                        )
                except httpx.RequestError:
                    continue

        except Exception as e:
            return ProbeResult(
                probe_name="generic_api",
                success=False,
                error_message=str(e),
            )

        return ProbeResult(
            probe_name="generic_api",
            success=True,
            detected_platform=None,
            confidence=0.0,
            details={"reason": "No API endpoints accessible"},
        )
