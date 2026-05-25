param(
    [Alias("Date")]
    [string]$SourceDate = "",
    [string]$Product = "",
    [Nullable[int]]$AssemblyLimit = $null,
    [int]$VariantsPerClip = 0,
    [string]$PythonExe = "python",
    [switch]$NoProductZoom,
    [switch]$ForceRescore,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSCommandPath

function Get-AssemblyConfig {
    param([string]$PythonCommand)

    $Code = @'
import json
from pathlib import Path
import config as cfg

payload = {
    "module_library_dir": getattr(cfg, "MODULE_LIBRARY_DIR", r"D:\proya_modules"),
    "output_dir": str(Path(getattr(cfg, "OUTPUT_DIR", r"D:\output_clips")) / "modular_assembly"),
    "working_dir": str(Path(getattr(cfg, "WORKING_DIR", "working")) / "modular_assembly"),
    "variants_per_clip": int(getattr(cfg, "VARIANTS_PER_CLIP", 1) or 1),
    "variant_seed": int(getattr(cfg, "VARIANT_SEED", 42) or 42),
    "render_limit": int(getattr(cfg, "MODULE_ASSEMBLY_RENDER_LIMIT", 3) or 0),
    "candidate_pool": int(getattr(cfg, "MODULE_ASSEMBLY_CANDIDATE_POOL", 30) or 0),
    "max_per_product": int(getattr(cfg, "MODULE_ASSEMBLY_MAX_PER_PRODUCT", 1) or 0),
    "same_date_only": True,
    "scorer_enabled": True,
    "auto_sort_enabled": True,
    "vision_scoring_enabled": bool(getattr(cfg, "SCORER_VISION_ENABLED", False)),
}
print(json.dumps(payload))
'@

    Push-Location $ProjectRoot
    try {
        $JsonText = $Code | & $PythonCommand -
        if ($LASTEXITCODE -ne 0) {
            throw "Could not load module assembly defaults from config.py"
        }
        return ($JsonText -join "`n") | ConvertFrom-Json
    }
    finally {
        Pop-Location
    }
}

$AssemblyConfig = Get-AssemblyConfig -PythonCommand $PythonExe
$EffectiveVariants = if ($VariantsPerClip -gt 0) { $VariantsPerClip } else { [int]$AssemblyConfig.variants_per_clip }
$EffectiveLimitLabel = if ($null -ne $AssemblyLimit) { "$AssemblyLimit per date" } else { "all eligible candidates per date" }
$ProductZoomEnabled = -not $NoProductZoom

$PythonArgs = @("-")
if (-not [string]::IsNullOrWhiteSpace($SourceDate)) {
    $PythonArgs += @("--date", $SourceDate)
}
if (-not [string]::IsNullOrWhiteSpace($Product)) {
    $PythonArgs += @("--product", $Product)
}
if ($null -ne $AssemblyLimit) {
    $PythonArgs += @("--limit", "$AssemblyLimit")
}
if ($VariantsPerClip -gt 0) {
    $PythonArgs += @("--variants-per-clip", "$VariantsPerClip")
}
if ($ProductZoomEnabled) {
    $PythonArgs += "--module-product-zoom"
}
if ($ForceRescore) {
    $PythonArgs += "--force-rescore"
}

$Driver = @'
import argparse
import json
import re

parser = argparse.ArgumentParser()
parser.add_argument("--date", default=None)
parser.add_argument("--product", default=None)
parser.add_argument("--limit", type=int, default=None)
parser.add_argument("--variants-per-clip", type=int, default=None)
parser.add_argument("--module-product-zoom", action="store_true")
parser.add_argument("--force-rescore", action="store_true")
args = parser.parse_args()

import config as cfg

# This wrapper is intentionally assembly-only. It enables the standalone
# module path for this process without changing config.py on disk.
cfg.MODULE_ASSEMBLY_ENABLED = True
cfg.MODULE_ASSEMBLY_SAME_DATE_ONLY = True
cfg.MODULE_ASSEMBLY_CANDIDATE_POOL = 0
cfg.MODULE_ASSEMBLY_MAX_PER_PRODUCT = 0
cfg.MODULE_ASSEMBLY_MIN_SOURCE_VIDEOS = 1
cfg.MODULAR_ASSEMBLY_READY_MIN_HOOK = 1
cfg.MODULAR_ASSEMBLY_READY_MIN_MAIN = 1
cfg.MODULAR_ASSEMBLY_READY_MIN_CTA = 1
cfg.SCORER_ENABLED = True
cfg.SCORER_AUTO_SORT_ENABLED = True

if args.force_rescore:
    cfg.SCORER_FORCE_RESCORE = True
if args.variants_per_clip is not None and args.variants_per_clip > 0:
    cfg.VARIANTS_PER_CLIP = args.variants_per_clip

from main import run_module_assembly
from module_assembler import _load_index_modules_with_words, _module_source_date
from module_extractor import canonical_product, read_library_index

def discover_source_dates():
    index = read_library_index(cfg)
    modules = _load_index_modules_with_words(index, cfg)
    product_filter = canonical_product(args.product) if args.product else None
    dates = set()
    for module in modules:
        if product_filter and canonical_product(module.get("product")) != product_filter:
            continue
        source_date = _module_source_date(module, warn=False)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", source_date or ""):
            dates.add(source_date)
    return sorted(dates)

dates = [args.date] if args.date else discover_source_dates()
if not dates:
    print(json.dumps({
        "dates_processed": 0,
        "message": "No dated modules found in the library for the requested filters.",
    }, indent=2))
    raise SystemExit(0)

per_date_limit = args.limit if args.limit is not None else 2_147_483_647
results = []
for source_date in dates:
    result = run_module_assembly(
        assembly_date=source_date,
        product=args.product,
        module_assembly_limit=per_date_limit,
        module_product_zoom=args.module_product_zoom,
    )
    results.append(result)

summary = {
    "dates_processed": len(results),
    "dates": [result.get("source_date_filter") for result in results],
    "product_filter": args.product,
    "per_date_limit": args.limit if args.limit is not None else "all",
    "candidates": sum(int(result.get("jobs", 0) or 0) for result in results),
    "render_jobs": sum(int(result.get("render_jobs", result.get("jobs", 0)) or 0) for result in results),
    "created": sum(int(result.get("created", 0) or 0) for result in results),
    "skipped": sum(int(result.get("skipped", 0) or 0) for result in results),
    "blocked": sum(int(result.get("blocked", 0) or 0) for result in results),
    "failed": sum(int(result.get("failed", 0) or 0) for result in results),
    "clips_scored": sum(int(result.get("clips_scored", 0) or 0) for result in results),
    "outputs": [
        {
            "date": result.get("source_date_filter"),
            "output_dir": result.get("output_dir"),
            "manifest_path": result.get("manifest_path"),
            "scores_summary_path": result.get("scores_summary_path"),
            "created": result.get("created", 0),
            "failed": result.get("failed", 0),
            "clips_scored": result.get("clips_scored", 0),
        }
        for result in results
    ],
}
print(json.dumps(summary, indent=2, ensure_ascii=False))
'@

Write-Host "Starting PROYA module assembly only"
Write-Host "Project: $ProjectRoot"
Write-Host "Library: $($AssemblyConfig.module_library_dir)"
Write-Host "Output root: $($AssemblyConfig.output_dir)"
Write-Host "Working root: $($AssemblyConfig.working_dir)"
Write-Host "Same-date-only: enabled"
Write-Host "Date mode: $(if ([string]::IsNullOrWhiteSpace($SourceDate)) { 'all discovered source dates' } else { 'single requested source date' })"
Write-Host "Variants per assembly: $EffectiveVariants"
Write-Host "Assembly limit: $EffectiveLimitLabel"
Write-Host "Candidate pool: unlimited"
Write-Host "Max per product: unlimited"
Write-Host "Per-date readiness: at least 1 hook, 1 main, 1 CTA"
Write-Host "Scoring: enabled"
Write-Host "Auto-sort: enabled"
Write-Host "Vision scoring: $($AssemblyConfig.vision_scoring_enabled)"
Write-Host "Product zoom: $ProductZoomEnabled"
if (-not [string]::IsNullOrWhiteSpace($SourceDate)) {
    Write-Host "Source date filter: $SourceDate"
}
if (-not [string]::IsNullOrWhiteSpace($Product)) {
    Write-Host "Product filter: $Product"
}
if ($ForceRescore) {
    Write-Host "Force rescore: enabled"
}

if ($DryRun) {
    Write-Host "$PythonExe $($PythonArgs -join ' ')"
    exit 0
}

Push-Location $ProjectRoot
try {
    $Driver | & $PythonExe @PythonArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
