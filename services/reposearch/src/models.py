"""Pydantic models for request/response schemas."""

from typing import Optional

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    repo: str = Field(..., description="GitHub repo in owner/name format")
    query: str = Field(..., description="Natural language search query")
    branch: str = Field("main", description="Branch to search")
    top_k: int = Field(10, ge=1, le=50, description="Number of results to return")


class ReindexRequest(BaseModel):
    branch: str = Field("main", description="Branch to re-index")


class SearchResult(BaseModel):
    file_path: str
    chunk: str
    chunk_type: str
    score: float


class SearchResponse(BaseModel):
    repo: str
    query: str
    results: list[SearchResult]
    index_sha: str


class IndexingResponse(BaseModel):
    repo: str
    status: str = "indexing"
    message: str = "First-time indexing in progress. Retry in ~30 seconds."
    estimated_files: Optional[int] = None


class RepoStatusResponse(BaseModel):
    repo: str
    status: str
    branch: str
    last_indexed_sha: Optional[str] = None
    last_indexed_at: Optional[str] = None
    file_count: Optional[int] = None
    chunk_count: Optional[int] = None


class ErrorResponse(BaseModel):
    error: str
    service: Optional[str] = None
    message: Optional[str] = None
    documentation_url: Optional[str] = None
