from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import config

from clipper_app.application.api_security import ApiSecuritySettings
from clipper_app.application.read_services import ReadDashboardService
from clipper_app.application.settings import LegacyConfigProvider
from clipper_app.contracts.modular_planner_models import ModularPlannerRunCreateRequest, SUGGESTED_DURATION_DEFAULTS
from clipper_app.modular_planner.library_reader import ScannerLibraryReader
from clipper_app.modular_planner.repository import PlannerRepository
from clipper_app.modular_planner.selection import ModularPlannerSelector
from clipper_app.modular_planner.service import ModularPlannerService


PRODUCTS = ("cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream")
TEMPLATES = ("standard", "ingredient", "benefit_focus")
CTA_MODES = ("use_cta", "no_cta")


def percentile(values: list[float], percentile_value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile_value))))
    return ordered[index]


def benchmark(iterations: int) -> dict[str, object]:
    root = ROOT
    library = ScannerLibraryReader(root / "working" / "modular_library.sqlite3", getattr(config, "QUEUE_INPUT_DIR"))
    selector = ModularPlannerSelector()
    core_times: list[float] = []
    generated_counts: list[int] = []
    for iteration in range(iterations):
        for product in PRODUCTS:
            inventory = library.inventory(product)
            for template in TEMPLATES:
                for cta_mode in CTA_MODES:
                    minimum, maximum = SUGGESTED_DURATION_DEFAULTS[(template, cta_mode)]
                    started = time.perf_counter()
                    compositions, _warnings, _statistics = selector.generate(
                        segments=inventory["segments"], product=product,
                        requested_template=template, actual_template=template, cta_mode=cta_mode,
                        target_min_duration=minimum, target_max_duration=maximum,
                        requested_count=100, starting_ordinal=1,
                        seed=f"benchmark-{iteration}-{product}-{template}-{cta_mode}",
                        approved_usage={}, current_run_usage={}, comparisons=[],
                    )
                    core_times.append(time.perf_counter() - started)
                    generated_counts.append(len(compositions))

    endpoint_times: list[float] = []
    with tempfile.TemporaryDirectory() as temp:
        from fastapi.testclient import TestClient
        from clipper_app.web_api import create_app

        cfg = SimpleNamespace(**{
            name: getattr(config, name) for name in dir(config)
            if name.isupper() and not name.startswith("__")
        })
        cfg.WORKING_DIR = str(root / "working")
        planner = ModularPlannerService(
            cfg,
            library=library,
            repository=PlannerRepository(Path(temp) / "modular_planner.sqlite3"),
        )
        reads = ReadDashboardService(LegacyConfigProvider(cfg))
        scanner_stub = SimpleNamespace(close=lambda: None)
        security = ApiSecuritySettings(
            token="benchmark", actor="benchmark", desktop=False,
            allowed_hosts=("testserver",), allowed_origins=(),
        )
        app = create_app(
            reads,
            security_settings=security,
            modular_scanner_service=scanner_stub,
            modular_planner_service=planner,
        )
        with TestClient(app, headers={"Authorization": "Bearer benchmark"}) as client:
            for iteration in range(iterations):
                request = ModularPlannerRunCreateRequest(
                    product="serum", requested_count=100, requested_template="standard",
                    cta_mode="use_cta", target_min_duration=45, target_max_duration=75,
                    ingredient_shortage_policy="partial", seed=f"endpoint-{iteration}",
                )
                started = time.perf_counter()
                response = client.post("/api/modular-planner/runs", json=request.model_dump(mode="json"))
                elapsed = time.perf_counter() - started
                if response.status_code != 201:
                    raise RuntimeError(f"Endpoint benchmark failed: {response.status_code} {response.text[:500]}")
                endpoint_times.append(elapsed)

    core_p95 = percentile(core_times, 0.95)
    endpoint_p95 = percentile(endpoint_times, 0.95)
    return {
        "iterations": iterations,
        "core_cases": len(core_times),
        "core_p50_seconds": statistics.median(core_times),
        "core_p95_seconds": core_p95,
        "core_worst_seconds": max(core_times),
        "endpoint_p50_seconds": statistics.median(endpoint_times),
        "endpoint_p95_seconds": endpoint_p95,
        "endpoint_worst_seconds": max(endpoint_times),
        "minimum_generated_in_core_case": min(generated_counts),
        "synchronous_release_gate_passed": core_p95 < 2.0 and max(endpoint_times) < 5.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(benchmark(max(1, args.iterations)), indent=2))


if __name__ == "__main__":
    main()
