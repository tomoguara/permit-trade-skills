"""Embedding service for permit descriptions."""

import os
from typing import List, Optional
import warnings

from permit_trade_skills.config import get_hf_token, load_env

# Try to import sentence-transformers
try:
    from sentence_transformers import SentenceTransformer
    has_transformers = True
except ImportError:
    has_transformers = False
    SentenceTransformer = None


class EmbeddingService:
    """Service for computing embeddings for permit descriptions."""
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize the embedding service.
        
        Args:
            model_name: Name of the sentence-transformers model to use.
                       Default is 'all-MiniLM-L6-v2' (384-dimensional, fast).
        """
        self.model_name = model_name
        self._model: Optional[SentenceTransformer] = None
        self._disabled = False
        
    @property
    def model(self) -> Optional[SentenceTransformer]:
        """Lazy load the embedding model."""
        if self._model is not None:
            return self._model
        if self._disabled:
            # A previous load attempt failed; don't retry (or re-warn) per call.
            return None
            
        if not has_transformers:
            self._disabled = True
            warnings.warn(
                "sentence-transformers not installed. Embeddings will be disabled. "
                "Install with: pip install sentence-transformers"
            )
            return None
            
        try:
            # HF_TOKEN (from the environment or .env) authenticates Hub
            # downloads: higher rate limits, no "unauthenticated" warning.
            self._model = SentenceTransformer(self.model_name, token=get_hf_token())
            return self._model
        except Exception as e:
            self._disabled = True
            warnings.warn(f"Failed to load embedding model {self.model_name}: {e}")
            return None
            
    @property
    def is_available(self) -> bool:
        """Check if embedding service is available (triggers the lazy model load)."""
        return self.model is not None
        
    @property
    def dimension(self) -> int:
        """Get the embedding dimension."""
        if self.model:
            return self.model.get_sentence_embedding_dimension()
        return 384  # Default for all-MiniLM-L6-v2
    
    def compute(self, texts: List[str]) -> List[List[float]]:
        """
        Compute embeddings for a list of texts.
        
        Args:
            texts: List of text strings to embed.
            
        Returns:
            List of embedding vectors (each is a list of floats), or an empty
            list if the model is unavailable. Callers must treat an empty
            result as "no embeddings" and leave the column NULL; zero vectors
            are never returned because cosine distance against them is NaN.
        """
        if not texts:
            return []
            
        if not self.is_available:
            return []
            
        embeddings = self.model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            batch_size=32
        )
        return embeddings.tolist()
    
    def compute_single(self, text: str) -> List[float]:
        """
        Compute embedding for a single text.
        
        Args:
            text: Text string to embed.
            
        Returns:
            Embedding vector (list of floats), or an empty list if the model
            is unavailable.
        """
        result = self.compute([text])
        return result[0] if result else []


# Global instance for convenience
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Get or create the global embedding service instance."""
    global _embedding_service
    if _embedding_service is None:
        load_env()  # so EMBEDDING_MODEL / HF_TOKEN in .env apply outside the CLI too
        model_name = os.environ.get("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
        _embedding_service = EmbeddingService(model_name=model_name)
    return _embedding_service


def compute_embeddings(texts: List[str]) -> List[List[float]]:
    """ Convenience function to compute embeddings using the global service."""
    return get_embedding_service().compute(texts)


def compute_embedding(text: str) -> List[float]:
    """Convenience function to compute a single embedding."""
    return get_embedding_service().compute_single(text)
