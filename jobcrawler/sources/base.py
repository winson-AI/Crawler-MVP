from dataclasses import dataclass
from typing import Protocol
from typing import Optional


@dataclass
class JobPage:
    """One page of normalized jobs; page numbers start at 1."""
    page: int
    total: int
    jobs: list
    # Explicit upstream pagination boundary; total may count duplicate rows.
    has_more: Optional[bool] = None


class JobSource(Protocol):
    id: str
    name: str
    url: str

    def fetch_page(self, page: int) -> JobPage:
        ...
