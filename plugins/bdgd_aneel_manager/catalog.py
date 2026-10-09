from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path


GENERIC_TAGS = {"BDGD", "SIG-R", "DISTRIBUICAO", "DISTRIBUIÇÃO", "EXTRA"}


def _clean_token(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-zÀ-ÿ]+", "_", value.strip())
    return re.sub(r"_+", "_", value).strip("_").upper()


def _parse_modified(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class CatalogItem:
    item_id: str
    original_title: str
    distributor: str
    year: int
    reference_date: str
    modified_raw: str

    @property
    def friendly_name(self) -> str:
        return f"{self.distributor}_{self.year}"

    @property
    def download_url(self) -> str:
        return f"https://www.arcgis.com/sharing/rest/content/items/{self.item_id}/data"

    @property
    def modified(self) -> datetime:
        return _parse_modified(self.modified_raw)


def parse_row(row: dict[str, str]) -> CatalogItem | None:
    if (row.get("type") or "").strip().casefold() != "file geodatabase":
        return None
    title = (row.get("title") or "").strip()
    tags = [part.strip() for part in (row.get("tags") or "").split(",") if part.strip()]
    match = re.match(r"^(?P<name>.+?)_\d+_(?P<date>20\d{2}-\d{2}-\d{2})(?:_|$)", title, re.I)
    year = int(match.group("date")[:4]) if match else 0
    distributor = _clean_token(match.group("name")) if match else ""
    reference_date = match.group("date") if match else ""
    tag_date = next((tag for tag in tags if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", tag)), "")
    reference_date = tag_date or reference_date
    year = int(reference_date[:4]) if reference_date else year
    candidates = [_clean_token(tag) for tag in tags if _clean_token(tag) not in GENERIC_TAGS and not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", tag)]
    distributor = candidates[-1] if candidates else distributor
    item_id = (row.get("id") or "").strip()
    if not item_id or not distributor or not year:
        return None
    return CatalogItem(item_id, title, distributor, year, reference_date, (row.get("modified") or "").strip())


def load_catalog(path: str | Path) -> list[CatalogItem]:
    rows = None
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with Path(path).open("r", encoding=encoding, newline="") as handle:
                rows = list(csv.DictReader(handle))
            break
        except UnicodeDecodeError as exc:
            last_error = exc
    if rows is None:
        raise last_error or ValueError("Catálogo inválido")
    newest: dict[tuple[str, int], CatalogItem] = {}
    for row in rows:
        item = parse_row(row)
        if item is None:
            continue
        key = (item.distributor, item.year)
        if key not in newest or (item.reference_date, item.modified) > (newest[key].reference_date, newest[key].modified):
            newest[key] = item
    return sorted(newest.values(), key=lambda item: (item.distributor, -item.year))
