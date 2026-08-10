from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config as cfg
from whatsapp_backlog import (
    BacklogCoordinator,
    inventory,
    load_source_ledger,
    select_batches,
)
from whatsapp_media import MediaPolicy, validate_ffmpeg_capabilities


def _batch_range(value: str) -> tuple[int, int]:
    start, separator, end = value.partition(":")
    if not separator:
        raise argparse.ArgumentTypeError("batch range must be START:END")
    return int(start), int(end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory or build the permanent WhatsApp-compatible batch mirror."
    )
    parser.add_argument(
        "--source", default=r"D:\output_clips\export_batches"
    )
    parser.add_argument(
        "--destination", default=r"D:\output_clips\export_batches_whatsapp"
    )
    parser.add_argument("--source-ledger")
    parser.add_argument(
        "--validation-level",
        choices=("probe", "sample-decode", "full"),
        default="probe",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--resume-run")
    parser.add_argument(
        "--migrate-run-policy",
        action="store_true",
        help="Migrate the specified stopped resumable run to the current policy before resuming it",
    )
    parser.add_argument("--abandon-run")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--probe-workers", type=int, default=1)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--batch-range", type=_batch_range)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--stop-on-batch-failure", action="store_true")
    parser.add_argument("--adopt-existing", action="store_true")
    parser.add_argument("--threshold-bps", type=int)
    parser.add_argument("--report-json")
    parser.add_argument("--assumed-probe-rate", type=float)
    parser.add_argument("--assumed-decode-realtime-factor", type=float)
    parser.add_argument("--assumed-encode-realtime-factor", type=float)
    parser.add_argument("--bootstrap-future-packaging", action="store_true")
    parser.add_argument("--copy-forward-pending", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    policy = MediaPolicy.from_config(cfg)
    if args.threshold_bps:
        policy = type(policy)(
            **{**policy.__dict__, "threshold_720p_bps": args.threshold_bps}
        )
    all_batches, invalid = load_source_ledger(args.source, args.source_ledger)
    batches = select_batches(
        all_batches, batch=args.batch, batch_range=args.batch_range
    )
    if not batches:
        print(json.dumps({"error": "No matching numeric batches"}, indent=2))
        return 2
    report = inventory(
        batches,
        args.destination,
        policy=policy,
        validation_level=args.validation_level,
        invalid_numeric_batches=invalid,
    )
    inventory_payload = report.to_dict()
    probe_rate = args.assumed_probe_rate or (
        report.total_files / report.probe_seconds if report.probe_seconds > 0 else 1.0
    )
    decode_factor = args.assumed_decode_realtime_factor or 4.0
    encode_factor = args.assumed_encode_realtime_factor or 2.0
    transcode_count = int(report.classifications.get("transcode", 0))
    transcode_fraction = transcode_count / max(1, report.total_files)
    inventory_payload["estimated_processing_time_seconds"] = round(
        (report.total_files / max(0.01, probe_rate))
        + (
            report.total_duration_seconds
            / max(0.01, decode_factor)
        )
        + (
            report.total_duration_seconds
            * transcode_fraction
            / max(0.01, encode_factor)
            / max(1, args.workers)
        ),
        1,
    )
    inventory_payload["estimated_temporary_bytes"] = (
        report.estimated_temporary_bytes * max(1, args.workers)
    )
    inventory_payload["eta_assumptions"] = {
        "probe_files_per_second": probe_rate,
        "decode_realtime_factor": decode_factor,
        "encode_realtime_factor": encode_factor,
        "workers": max(1, args.workers),
    }
    payload: dict[str, object] = {"inventory": inventory_payload}
    if not args.execute:
        print(json.dumps(payload, indent=2))
        if args.report_json:
            Path(args.report_json).write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        return 0
    capabilities_ok, capability_errors = validate_ffmpeg_capabilities()
    if not capabilities_ok:
        payload["error"] = {"ffmpeg_capabilities": capability_errors}
        print(json.dumps(payload, indent=2))
        return 2
    relevant_options = {
        "workers": args.workers,
        "stop_on_batch_failure": args.stop_on_batch_failure,
        "threshold_bps": args.threshold_bps,
    }
    coordinator = BacklogCoordinator(
        args.source,
        args.destination,
        batches,
        policy=policy,
        workers=args.workers,
        stop_on_batch_failure=args.stop_on_batch_failure,
        adopt_existing=args.adopt_existing,
        relevant_options=relevant_options,
    )
    if args.copy_forward_pending:
        payload["error"] = (
            "--copy-forward-pending requires an operator-approved pending ledger; "
            "no pending files were changed"
        )
        print(json.dumps(payload, indent=2))
        return 2
    if args.bootstrap_future_packaging:
        floor = coordinator.state.set_packaging_floor(
            max(item.batch_number for item in all_batches)
        )
        payload["packaging_floor"] = floor
    if args.abandon_run:
        coordinator.abandon_run(args.abandon_run)
        payload["execution"] = {
            "run_id": args.abandon_run,
            "status": "abandoned",
        }
        print(json.dumps(payload, indent=2))
        return 0
    if args.migrate_run_policy:
        if not args.resume_run:
            payload["error"] = "--migrate-run-policy requires --resume-run"
            print(json.dumps(payload, indent=2))
            return 2
        payload["policy_migration"] = coordinator.migrate_run_policy(args.resume_run)
    resume_id = None
    if args.resume_run:
        resume_id = coordinator.find_resume_run(args.resume_run)
    elif args.resume:
        resume_id = coordinator.find_resume_run()
    payload["execution"] = coordinator.execute(resume_run=resume_id)
    print(json.dumps(payload, indent=2))
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
