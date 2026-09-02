from __future__ import annotations

import argparse
import json
from pathlib import Path

import config
from clipper_app.storage.inventory import DryRunReclamationPlanner, StorageInventoryService
from clipper_app.storage.migration_phase2a import Phase2AMigrator
from clipper_app.storage.reconciliation import ModularProductionReconciler, write_reconciliation_report
from clipper_app.storage.registry import ArtifactRegistry


PROJECT_ROOT = Path(__file__).resolve().parent


def _registry() -> ArtifactRegistry:
    return ArtifactRegistry.from_working_dir(getattr(config, "WORKING_DIR", "working"))


def configured_roots() -> dict[str, Path]:
    return {
        "project": PROJECT_ROOT,
        "working": Path(getattr(config, "WORKING_DIR", "working")),
        "output_clips": Path(getattr(config, "OUTPUT_DIR", r"D:\output_clips")),
        "vod": Path(getattr(config, "QUEUE_INPUT_DIR", r"D:\VOD")),
    }


def build_inventory_report() -> dict:
    app_root = PROJECT_ROOT / "new_app"
    rows = []
    for path in sorted(app_root.glob("dist*")) if app_root.exists() else []:
        size = sum(child.stat().st_size for child in path.rglob("*") if child.is_file()) if path.is_dir() else path.stat().st_size
        rows.append({"path": str(path.resolve()), "size_bytes": size, "kind": "directory" if path.is_dir() else "file"})
    for path in sorted(app_root.glob("*.exe")) if app_root.exists() else []:
        rows.append({"path": str(path.resolve()), "size_bytes": path.stat().st_size, "kind": "installer"})
    return {"dry_run": True, "automatic_deletion": False, "items": rows, "total_bytes": sum(row["size_bytes"] for row in rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clipper Phase 1 storage inventory and reconciliation")
    sub = parser.add_subparsers(dest="command", required=True)
    inventory = sub.add_parser("inventory", help="Run an explicit metadata-only inventory")
    inventory.add_argument("--root", action="append", choices=tuple(configured_roots()), dest="roots")
    inventory.add_argument("--max-files", type=int, default=None)
    inventory.add_argument("--output", default="reports/storage-inventory.json")
    plan = sub.add_parser("plan", help="Write a zero-deletion dry-run reclamation plan")
    plan.add_argument("--root", action="append", choices=tuple(configured_roots()), dest="roots")
    plan.add_argument("--max-files", type=int, default=None)
    plan.add_argument("--output", default="reports/storage-reclamation-plan.json")
    reconcile = sub.add_parser("reconcile", help="Reconcile modular production output paths")
    reconcile.add_argument("--dry-run", action="store_true")
    reconcile.add_argument("--output", default="reports/storage-reconciliation.md")
    builds = sub.add_parser("build-inventory", help="List development builds without deleting them")
    builds.add_argument("--output", default="reports/storage-build-inventory.json")
    phase2_plan = sub.add_parser("phase2a-plan", help="Create a resumable historical migration plan")
    phase2_plan.add_argument("--migration-id", default=None)
    phase2_backup = sub.add_parser("phase2a-backup", help="Create and verify backups for an existing Phase 2A plan")
    phase2_backup.add_argument("--migration-id", required=True)
    phase2_apply = sub.add_parser("phase2a-apply", help="Apply only strongly proven Phase 2A candidates")
    phase2_apply.add_argument("--migration-id", required=True)
    phase2_external = sub.add_parser("phase2a-external-inventory", help="Inventory external roots without deletion or hashing")
    phase2_external.add_argument("--migration-id", required=True)
    phase2_final = sub.add_parser("phase2a-final-inventory", help="Record final project ownership metrics")
    phase2_final.add_argument("--migration-id", required=True)
    args = parser.parse_args(argv)

    if args.command in {"inventory", "plan"}:
        selected = args.roots or ["project", "output_clips", "vod"]
        roots = {name: configured_roots()[name] for name in selected}
        snapshot = StorageInventoryService(_registry()).scan(roots, max_files=args.max_files)
        if args.command == "inventory":
            payload = {
                "scan_id": snapshot.scan_id, "generated_at": snapshot.generated_at,
                "roots": snapshot.roots, "truncated": snapshot.truncated,
                "total_bytes": snapshot.total_bytes,
                "records": [row.__dict__ for row in snapshot.records],
            }
            target = Path(args.output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            payload = DryRunReclamationPlanner(_registry()).plan(snapshot)
            DryRunReclamationPlanner.write(args.output, payload)
        print(json.dumps({
            key: payload[key]
            for key in payload
            if key not in {"records", "items", "blocked_examples"}
        }, indent=2))
        return 0
    if args.command == "reconcile":
        db = Path(getattr(config, "WORKING_DIR", "working")) / "modular_production.sqlite3"
        summary = ModularProductionReconciler(db, _registry()).reconcile(apply=not args.dry_run)
        write_reconciliation_report(args.output, summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "results"}, indent=2))
        return 0
    if args.command.startswith("phase2a-"):
        migrator = Phase2AMigrator(PROJECT_ROOT, args.migration_id)
        if args.command == "phase2a-plan":
            payload = migrator.discover()
        elif args.command == "phase2a-backup":
            migrator.create_backups()
            payload = migrator.journal.export(migrator.report_manifest)
        elif args.command == "phase2a-external-inventory":
            payload = migrator.inventory_external()
        elif args.command == "phase2a-final-inventory":
            payload = migrator.finalize_project_inventory()
        else:
            payload = migrator.apply()
        print(json.dumps({
            "migration_id": migrator.migration_id,
            "migration_root": str(migrator.migration_root),
            "manifest": str(migrator.report_manifest),
            "summary": payload["summary"],
        }, indent=2))
        return 0
    payload = build_inventory_report()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
