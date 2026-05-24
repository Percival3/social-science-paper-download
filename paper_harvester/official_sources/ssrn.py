"""SSRN official-source metadata helpers."""
import re
from typing import Dict, Optional


SSRN_PAPER_DOI = re.compile(r"^10\.2139/ssrn\.(\d+)$", re.IGNORECASE)


def source_for_doi(doi: str) -> Optional[Dict[str, str]]:
    """Return SSRN official-source metadata for supported DOIs."""
    match = SSRN_PAPER_DOI.match(doi.strip().lower())
    if not match:
        return None

    abstract_id = match.group(1)
    return {
        "source": "ssrn-official",
        "page_url": f"https://papers.ssrn.com/sol3/papers.cfm?abstract_id={abstract_id}",
        "abstract_id": abstract_id,
    }
