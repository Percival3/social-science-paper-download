"""Shared download result type used by all download backends."""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DownloadResult:
    """Result of one download attempt."""
    success: bool
    file_path: Optional[Path] = None
    file_size: Optional[int] = None
    sha256: Optional[str] = None
    mirror: Optional[str] = None
    scihub_url: Optional[str] = None
    pdf_url: Optional[str] = None
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    response_time_ms: Optional[int] = None
