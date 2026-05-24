"""Dispatch supported special DOI families to official-source downloaders."""
from pathlib import Path
from typing import Dict, Optional

from ..config import Config
from ..database import Database
from ..download_result import DownloadResult
from . import nber, ssrn


def official_source_for_doi(doi: str) -> Optional[Dict[str, str]]:
    """Return official-source metadata for a supported special DOI."""
    return nber.source_for_doi(doi) or ssrn.source_for_doi(doi)


def try_official_source(
    *,
    config: Config,
    db: Database,
    doi: str,
    output_path: Path,
) -> Optional[DownloadResult]:
    """Try official downloaders that are fully split out of scihub.py."""
    nber_result = NBEROfficialDownloader(config, db).download(doi, output_path)
    if nber_result is not None:
        return nber_result

    return None


NBEROfficialDownloader = nber.NBEROfficialDownloader
