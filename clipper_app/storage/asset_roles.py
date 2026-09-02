from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class AssetRole:
    asset_id: str
    content_sha256: str
    canonical_path: str
    visible_name: str
    role: str
    product: str | None
    ordering: int


def index_asset_roles(rows: Iterable[tuple[str | Path, str, str | None, int]]) -> list[AssetRole]:
    """Model many logical B-roll roles without changing existing physical trees."""
    identities: dict[str, tuple[str, Path]] = {}
    output: list[AssetRole] = []
    for raw_path, role, product, ordering in rows:
        path = Path(raw_path).resolve(strict=True)
        digest = _sha256(path)
        asset_id = f"asset_{digest}"
        canonical = identities.setdefault(digest, (asset_id, path))[1]
        output.append(AssetRole(
            asset_id=asset_id,
            content_sha256=digest,
            canonical_path=str(canonical),
            visible_name=path.name,
            role=str(role),
            product=str(product) if product else None,
            ordering=int(ordering),
        ))
    return output


def asset_role_payload(rows: Iterable[AssetRole]) -> list[dict]:
    return [asdict(row) for row in sorted(rows, key=lambda item: (item.role, item.product or "", item.ordering, item.visible_name.casefold()))]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
