"""
Embedding models for vertector-scylladbstore.

Provides LangChain-compatible embedding classes using open-source models.
"""

import asyncio
from typing import List

from sentence_transformers import SentenceTransformer


class QwenEmbeddings:
    """
    Qwen3 Embedding model wrapper compatible with LangChain's Embeddings interface.

    Uses the Qwen/Qwen3-Embedding-0.6B model via sentence-transformers.
    This is a free, open-source alternative to paid embedding APIs.

    Args:
        model_name: HuggingFace model name. Defaults to "Qwen/Qwen3-Embedding-0.6B".
        device: Device to run the model on ("cpu", "cuda", "mps"). Defaults to auto-detect.

    Example:
        >>> embeddings = QwenEmbeddings()
        >>> vector = embeddings.embed_query("What is machine learning?")
        >>> vectors = embeddings.embed_documents(["Doc 1", "Doc 2"])
    """

    def __init__(
        self,
        model_name: str = "Qwen/Qwen3-Embedding-0.6B",
        device: str | None = None,
    ):
        self.model_name = model_name
        self._model: SentenceTransformer | None = None
        self._device = device

    @property
    def model(self) -> SentenceTransformer:
        """Lazy load the model on first use."""
        if self._model is None:
            self._model = SentenceTransformer(
                self.model_name,
                device=self._device,
            )
        return self._model

    @property
    def dims(self) -> int:
        """Return the embedding dimensions."""
        return self.model.get_sentence_embedding_dimension()

    def embed_query(self, text: str) -> List[float]:
        """
        Embed a single query text.

        Uses the "query" prompt for better retrieval performance.

        Args:
            text: Query text to embed.

        Returns:
            List of floats representing the embedding vector.
        """
        embedding = self.model.encode(text, prompt_name="query")
        return embedding.tolist()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Embed multiple documents.

        Documents are embedded without a prompt (asymmetric retrieval).

        Args:
            texts: List of document texts to embed.

        Returns:
            List of embedding vectors.
        """
        embeddings = self.model.encode(texts)
        return [emb.tolist() for emb in embeddings]

    async def aembed_query(self, text: str) -> List[float]:
        """
        Async version of embed_query.

        Runs the embedding in a thread pool to avoid blocking.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.embed_query, text)

    async def aembed_documents(self, texts: List[str]) -> List[List[float]]:
        """
        Async version of embed_documents.

        Runs the embedding in a thread pool to avoid blocking.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.embed_documents, texts)
