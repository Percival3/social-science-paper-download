"""Official-source downloaders for special DOI families."""
from .dispatch import official_source_for_doi, try_official_source

__all__ = ["official_source_for_doi", "try_official_source"]
