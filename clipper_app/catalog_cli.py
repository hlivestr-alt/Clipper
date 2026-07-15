from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from clipper_app.application.catalog import CatalogDatabase, CatalogIndexer
from clipper_app.application.queue_repository import QueueStateRepository, queue_storage_mode
from clipper_app.application.settings import LegacyConfigProvider


def _services() -> tuple[Any, CatalogDatabase, CatalogIndexer, QueueStateRepository]:
    cfg = LegacyConfigProvider().live_view()
    database = CatalogDatabase.from_config(cfg)
    indexer = CatalogIndexer(database, cfg)
    state_path = Path(str(getattr(cfg, "QUEUE_STATE_FILE", "working/video_queue_state.json")))
    queue = QueueStateRepository(state_path, database=database, mode="sqlite")
    return cfg, database, indexer, queue


def _backup(database: CatalogDatabase, destination: Path | None = None) -> Path:
    database.ensure_schema()
    target = destination or database.path.with_name(
        f"{database.path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.backup.sqlite3"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    source = database.connect()
    backup = __import__("sqlite3").connect(target)
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clipper catalog and queue storage maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("backfill")
    subparsers.add_parser("verify")
    subparsers.add_parser("reconcile")
    migrate = subparsers.add_parser("migrate-queue")
    migrate.add_argument("--state-path")
    export = subparsers.add_parser("export-legacy-queue")
    export.add_argument("destination")
    backup = subparsers.add_parser("backup")
    backup.add_argument("destination", nargs="?")
    subparsers.add_parser("rebuild")
    args = parser.parse_args(argv)

    cfg, database, indexer, queue = _services()
    if args.command == "status":
        queue_status = queue.status()
        queue_status["runtime_mode"] = queue_storage_mode()
        queue_status["maintenance_mode"] = queue_status.pop("mode")
        payload = {"catalog": database.status(), "queue": queue_status}
    elif args.command in {"backfill", "reconcile"}:
        payload = {
            "backfill": indexer.backfill(force=args.command == "reconcile"),
            "verify": indexer.verify(),
        }
    elif args.command == "verify":
        payload = {
            "catalog": database.status(),
            "verify": indexer.verify(),
            "queue_history": queue.verify_history(),
        }
    elif args.command == "migrate-queue":
        state_path = Path(args.state_path) if args.state_path else Path(str(getattr(cfg, "QUEUE_STATE_FILE")))
        backup_dir = state_path.resolve().parent / "queue_migration_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{state_path.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{state_path.suffix}"
        shutil.copy2(state_path, backup_path)
        digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        payload = {
            **queue.import_legacy(state),
            "backup": str(backup_path),
            "backup_sha256": digest,
            "history_integrity": queue.verify_history(),
        }
    elif args.command == "export-legacy-queue":
        payload = {"destination": str(queue.export_legacy(args.destination))}
    elif args.command == "backup":
        payload = {"backup": str(_backup(database, Path(args.destination) if args.destination else None))}
    elif args.command == "rebuild":
        quarantine_dir = database.path.parent / "quarantine" / datetime.now().strftime("%Y%m%d-%H%M%S")
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantined: list[str] = []
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{database.path}{suffix}")
            if path.exists():
                destination = quarantine_dir / path.name
                shutil.move(str(path), destination)
                quarantined.append(str(destination))
        database._migrated = False
        database.ensure_schema()
        recovered = queue.load()
        payload = {
            "quarantined": quarantined,
            "queue_recovered": len(dict(recovered.get("videos") or {})),
            "backfill": indexer.backfill(),
            "verify": indexer.verify(),
            "queue_history": queue.verify_history(),
        }
    else:  # pragma: no cover
        parser.error("unknown command")
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
