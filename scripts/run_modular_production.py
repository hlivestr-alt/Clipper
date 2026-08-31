from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import config
from clipper_app.application.services import ComplianceService, ExportPackagingService, ScoringService
from clipper_app.contracts.modular_production_models import ModularProductionJobCreateRequest
from clipper_app.modular_planner import ModularPlannerService
from clipper_app.modular_production import ModularProductionService
from clipper_app.modular_renderer import ModularRendererService
from clipper_app.modular_variants import ModularVariantService


TERMINAL = {"completed", "completed_with_failures", "failed", "cancelled"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one persisted Modular Video production job")
    parser.add_argument("--product", default="serum")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--template", default="standard")
    parser.add_argument("--cta", default="use_cta")
    parser.add_argument("--minimum", type=float, default=45)
    parser.add_argument("--maximum", type=float, default=75)
    parser.add_argument("--profile", default="active")
    parser.add_argument("--explicit-rerun", action="store_true")
    args = parser.parse_args()

    planner = ModularPlannerService(config)
    renderer = ModularRendererService(config, planner=planner)
    variants = ModularVariantService(config, renderer=renderer, planner=planner)
    production = ModularProductionService(
        config,
        planner=planner,
        renderer=renderer,
        variants=variants,
        compliance=ComplianceService(),
        scoring=ScoringService(),
        exports=ExportPackagingService(),
    )
    try:
        job, reused = production.create_job(ModularProductionJobCreateRequest(
            workflow_mode="automatic",
            product=args.product,
            requested_base_count=args.count,
            requested_template=args.template,
            cta_mode=args.cta,
            target_min_duration=args.minimum,
            target_max_duration=args.maximum,
            ingredient_shortage_policy="partial",
            variant_profile_id=args.profile,
            explicit_rerun=args.explicit_rerun,
        ))
        print(json.dumps({"job_id": job["job_id"], "reused": reused}), flush=True)
        last = None
        while True:
            job = production.get_job(job["job_id"])
            snapshot = (
                job["status"], job["current_stage"], round(job["stage_progress"], 1),
                job["rendered_base_count"], job["generated_variant_count"], job["exported_count"],
            )
            if snapshot != last:
                print(json.dumps({
                    "status": snapshot[0], "stage": snapshot[1], "progress": snapshot[2],
                    "rendered_bases": snapshot[3], "generated_variants": snapshot[4],
                    "exported": snapshot[5],
                }), flush=True)
                last = snapshot
            if job["status"] in TERMINAL:
                print(json.dumps(job, ensure_ascii=False, indent=2), flush=True)
                return 0 if job["status"] in {"completed", "completed_with_failures"} else 1
            time.sleep(2)
    finally:
        production.close()
        renderer.close()


if __name__ == "__main__":
    raise SystemExit(main())
