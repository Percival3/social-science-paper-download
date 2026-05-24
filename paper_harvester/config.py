"""
Configuration management for Paper Harvester.
"""
import os
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass
class Config:
    """Application configuration."""
    
    # Required
    crossref_mailto: str = ""
    user_agent: str = "paper-harvester/1.0"
    
    # Sci-Hub mirrors
    scihub_mirrors: List[str] = field(default_factory=list)
    scihub_timeout: int = 30
    scihub_retry: int = 3
    scihub_mirror_cooldown: int = 300

    # Official publisher/source downloads
    official_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
    nber_cookie: Optional[str] = None
    ssrn_cookie: Optional[str] = None
    
    # System
    data_dir: Path = field(default_factory=lambda: Path("data"))
    requests_per_minute: int = 10
    concurrent_downloads: int = 3
    max_retries_per_doi: int = 5
    
    # Proxy
    http_proxy: Optional[str] = None
    https_proxy: Optional[str] = None
    
    def __post_init__(self):
        """Convert data_dir to Path if it's a string."""
        if isinstance(self.data_dir, str):
            self.data_dir = Path(self.data_dir)
    
    @property
    def state_dir(self) -> Path:
        """Path to state directory (contains database)."""
        return self.data_dir / "state"
    
    @property
    def metadata_dir(self) -> Path:
        """Path to metadata directory."""
        return self.data_dir / "metadata"
    
    @property
    def pdf_dir(self) -> Path:
        """Path to PDF storage directory."""
        return self.data_dir / "fulltext" / "pdf"
    
    @property
    def txt_dir(self) -> Path:
        """Path to extracted text directory."""
        return self.data_dir / "fulltext" / "txt"
    
    @property
    def manifests_dir(self) -> Path:
        """Path to manifests/reports directory."""
        return self.data_dir / "manifests"
    
    @property
    def logs_dir(self) -> Path:
        """Path to logs directory."""
        return self.data_dir / "logs"
    
    @property
    def db_path(self) -> Path:
        """Path to SQLite database."""
        return self.state_dir / "papers.sqlite"
    
    @property
    def download_log_path(self) -> Path:
        """Path to download log file."""
        return self.logs_dir / "download.log"
    
    @property
    def error_log_path(self) -> Path:
        """Path to error log file."""
        return self.logs_dir / "errors.log"
    
    def ensure_directories(self) -> None:
        """Create all necessary directories."""
        for dir_path in [
            self.state_dir,
            self.metadata_dir,
            self.pdf_dir,
            self.txt_dir,
            self.manifests_dir,
            self.logs_dir,
        ]:
            dir_path.mkdir(parents=True, exist_ok=True)


def load_config(env_file: Optional[str] = None) -> Config:
    """
    Load configuration from environment variables.
    
    Args:
        env_file: Optional path to .env file
        
    Returns:
        Config object with loaded values
    """
    # Load .env file if provided or exists in current directory
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()
    
    # Parse Sci-Hub mirrors
    mirrors_str = os.getenv("SCIHUB_MIRRORS", "")
    mirrors = [m.strip() for m in mirrors_str.split(",") if m.strip()]
    
    # Default mirrors if none configured
    if not mirrors:
        mirrors = [
            "https://sci-hub.se",
            "https://sci-hub.st",
            "https://sci-hub.ru",
            "https://sci-hub.wf",
            "https://sci-hub.ren",
        ]
    
    return Config(
        crossref_mailto=os.getenv("CROSSREF_MAILTO", ""),
        user_agent=os.getenv("USER_AGENT", "paper-harvester/1.0"),
        scihub_mirrors=mirrors,
        scihub_timeout=int(os.getenv("SCIHUB_TIMEOUT", "30")),
        scihub_retry=int(os.getenv("SCIHUB_RETRY", "3")),
        scihub_mirror_cooldown=int(os.getenv("SCIHUB_MIRROR_COOLDOWN", "300")),
        official_user_agent=os.getenv(
            "OFFICIAL_USER_AGENT",
            (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        ),
        nber_cookie=os.getenv("NBER_COOKIE") or None,
        ssrn_cookie=os.getenv("SSRN_COOKIE") or None,
        data_dir=Path(os.getenv("DATA_DIR", "data")),
        requests_per_minute=int(os.getenv("REQUESTS_PER_MINUTE", "10")),
        concurrent_downloads=int(os.getenv("CONCURRENT_DOWNLOADS", "3")),
        max_retries_per_doi=int(os.getenv("MAX_RETRIES_PER_DOI", "5")),
        http_proxy=os.getenv("HTTP_PROXY") or None,
        https_proxy=os.getenv("HTTPS_PROXY") or None,
    )


def validate_config(config: Config) -> List[str]:
    """
    Validate configuration and return list of error messages.
    
    Args:
        config: Config object to validate
        
    Returns:
        List of error messages (empty if valid)
    """
    errors = []
    
    if not config.crossref_mailto:
        errors.append("CROSSREF_MAILTO is required (set it in .env file)")
    
    if not config.scihub_mirrors:
        errors.append("At least one Sci-Hub mirror must be configured")
    
    if config.requests_per_minute < 1:
        errors.append("REQUESTS_PER_MINUTE must be at least 1")
    
    if config.concurrent_downloads < 1:
        errors.append("CONCURRENT_DOWNLOADS must be at least 1")
    
    return errors
