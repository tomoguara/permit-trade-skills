"""Candidate scoring and ranking for discovery results."""

import re
from typing import Any, Dict, List, Optional

from permit_trade_skills.discovery.schemas import DiscoveryCandidate, PlatformType


class Scorer:
    """Scores and ranks discovery candidates."""

    # Weight for each scoring factor
    WEIGHTS = {
        "title_match": 0.35,
        "organization_match": 0.20,
        "keyword_match": 0.20,
        "platform_confidence": 0.15,
        "metadata_quality": 0.10,
    }

    def __init__(self, jurisdiction_name: str, state: str):
        self.jurisdiction_name = jurisdiction_name.lower().strip()
        self.state = state.lower().strip()
        self.stop_words = {"city", "county", "of", "the", "for", "and", "in"}

    def factor_scores(self, candidate: DiscoveryCandidate) -> Dict[str, float]:
        """Each factor's 0-1 score (before weighting)."""
        return {
            "title_match": self._score_title(candidate),
            "organization_match": self._score_organization(candidate),
            "keyword_match": self._score_keywords(candidate),
            "platform_confidence": self._score_platform(candidate),
            "metadata_quality": self._score_metadata_quality(candidate),
        }

    def score_candidate(self, candidate: DiscoveryCandidate) -> float:
        """
        Score a single candidate and record the per-factor breakdown in
        `candidate.score_breakdown` ({factor: {"score", "weight", "weighted"}}).

        Args:
            candidate: The candidate to score

        Returns:
            Score between 0.0 and 1.0
        """
        factors = self.factor_scores(candidate)
        candidate.score_breakdown = {
            name: {"score": round(value, 4), "weight": self.WEIGHTS[name],
                   "weighted": round(value * self.WEIGHTS[name], 4)}
            for name, value in factors.items()
        }
        score = sum(value * self.WEIGHTS[name] for name, value in factors.items())
        return min(1.0, max(0.0, score))

    def score_and_rank(self, candidates: List[DiscoveryCandidate]) -> List[DiscoveryCandidate]:
        """
        Score and rank a list of candidates.

        Args:
            candidates: List of candidates to score

        Returns:
            Sorted list with scores added
        """
        for candidate in candidates:
            candidate.confidence = self.score_candidate(candidate)

        # Sort by score descending
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    def _score_title(self, candidate: DiscoveryCandidate) -> float:
        """Score based on title match with jurisdiction."""
        title = candidate.title.lower()
        score = 0.0

        # Exact match
        if self.jurisdiction_name in title:
            score += 1.0

        # Partial match
        elif self._fuzzy_match(self.jurisdiction_name, title):
            score += 0.7

        # Check for common patterns
        patterns = [
            rf"\b{self.jurisdiction_name}\b.*permit",
            rf"permit.*\b{self.jurisdiction_name}\b",
            rf"{self.jurisdiction_name}.*build",
            rf"build.*{self.jurisdiction_name}",
        ]

        for pattern in patterns:
            if re.search(pattern, title, re.IGNORECASE):
                score = max(score, 0.8)

        # Penalize if state is missing
        if self.state not in title:
            score *= 0.9

        return min(1.0, score)

    def _score_organization(self, candidate: DiscoveryCandidate) -> float:
        """Score based on organization name match."""
        org = candidate.organization.lower()
        score = 0.0

        if self.jurisdiction_name in org:
            score = 1.0
        elif self._fuzzy_match(self.jurisdiction_name, org):
            score = 0.6

        return score

    def _score_keywords(self, candidate: DiscoveryCandidate) -> float:
        """Score based on keyword presence in title and metadata."""
        title = candidate.title.lower()
        metadata = candidate.metadata.get("tags", [])
        tags = " ".join([t.get("name", "").lower() for t in metadata])

        combined = f"{title} {tags}"

        keywords = [
            self.jurisdiction_name,
            self.state,
            "permit",
            "building",
            "construction",
            "department",
        ]

        matches = sum(1 for kw in keywords if kw in combined)
        score = matches / len(keywords)

        return score

    def _score_platform(self, candidate: DiscoveryCandidate) -> float:
        """Score based on platform confidence."""
        if candidate.platform_type is None:
            return 0.0

        # Prefer Socrata and CKAN
        platform_weights = {
            PlatformType.SOCRATA: 1.0,
            PlatformType.CKAN: 0.9,
            PlatformType.OTHER: 0.5,
        }

        return platform_weights.get(candidate.platform_type, 0.0)

    def _score_metadata_quality(self, candidate: DiscoveryCandidate) -> float:
        """Score based on metadata completeness."""
        score = 0.0
        max_score = 0.0

        metadata = candidate.metadata

        # Check for description
        max_score += 1.0
        if metadata.get("notes"):
            score += 1.0

        # Check for tags
        max_score += 1.0
        if metadata.get("tags"):
            score += 1.0

        # Check for resources
        max_score += 1.0
        if metadata.get("resources"):
            score += 1.0

        # Check for temporal coverage
        max_score += 0.5
        if metadata.get("temporal_coverage"):
            score += 0.5

        # Check for update frequency
        max_score += 0.5
        if metadata.get("metadata_update_frequency"):
            score += 0.5

        return score / max_score if max_score > 0 else 0.0

    def _fuzzy_match(self, search: str, text: str) -> bool:
        """Check for fuzzy match between search and text."""
        # Remove stop words and special characters
        search_clean = re.sub(r"[^a-z0-9]", "", search.lower())
        text_clean = re.sub(r"[^a-z0-9]", "", text.lower())

        # Check for substring match
        return search_clean in text_clean or text_clean in search_clean
