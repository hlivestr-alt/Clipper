import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Film, Play, RefreshCw, RotateCcw, Trash2 } from "lucide-react";

import type {
  IngredientShortagePolicy,
  ModularCtaMode,
  ModularPlannerComposition,
  ModularPlannerInventory,
  ModularPlannerItem,
  ModularPlannerRun,
  ModularProductionJob,
  ModularProductionProduct,
  ModularProductionWorkflow,
  ModularProduct,
  ModularRenderItem,
  ModularRenderRun,
  ModularVariantEligibleBase,
  ModularVariantPilotRun,
  ModularVariantProfileRef,
  ModularTemplate
} from "../api";
import { getJson, query, sendJson } from "../api";
import { useApiQuery } from "../useApiQuery";
import "./modularPlanner.css";

const products: Array<{ value: ModularProduct; label: string }> = [
  { value: "cleanser", label: "Cleanser" }, { value: "toner", label: "Toner" },
  { value: "serum", label: "Serum" }, { value: "eye_cream", label: "Eye Cream" },
  { value: "mask", label: "Mask" }, { value: "skin_cream", label: "Skin Cream" }
];
const productionProducts: Array<{ value: ModularProductionProduct; label: string }> = [
  ...products, { value: "all_products", label: "All Products" }
];

function allocationFor(count: number, seed: string): Partial<Record<ModularProduct, number>> {
  const allocation = Object.fromEntries(products.map(({ value }) => [value, Math.floor(count / products.length)])) as Record<ModularProduct, number>;
  let hash = 2166136261;
  for (const character of new TextEncoder().encode(seed)) hash = Math.imul(hash ^ character, 16777619) >>> 0;
  for (let index = 0; index < count % products.length; index += 1) {
    const product = products[(hash % products.length + index) % products.length].value;
    allocation[product] += 1;
  }
  return allocation;
}

const templates: Array<{ value: ModularTemplate; label: string }> = [
  { value: "standard", label: "Standard" },
  { value: "ingredient", label: "Ingredient" },
  { value: "benefit_focus", label: "Benefit Focus" }
];

const suggested: Record<`${ModularTemplate}:${ModularCtaMode}`, [number, number]> = {
  "standard:use_cta": [45, 75], "standard:no_cta": [30, 60],
  "ingredient:use_cta": [60, 90], "ingredient:no_cta": [45, 75],
  "benefit_focus:use_cta": [60, 90], "benefit_focus:no_cta": [45, 75]
};

function label(value: string): string {
  return value.replace(/_/g, " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function time(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${(seconds - minutes * 60).toFixed(1).padStart(4, "0")}`;
}

function warningText(warning: Record<string, unknown>): string {
  if (warning.code === "role_inventory_requires_reuse") {
    return `${label(String(warning.role))} inventory requires reuse: ${warning.available_segments} segments for ${warning.requested_uses} requested uses.`;
  }
  if (warning.code === "search_exhausted") {
    return `Search generated ${warning.generated} of ${warning.requested}; shortfall ${warning.shortfall}.`;
  }
  if (warning.code === "missing_role_inventory") return `Missing required inventory: ${String(warning.roles)}.`;
  return String(warning.code ?? "Planner warning").replace(/_/g, " ");
}

export function ModularPlannerPage() {
  const [product, setProduct] = useState<ModularProduct>("serum");
  const [template, setTemplate] = useState<ModularTemplate>("standard");
  const [ctaMode, setCtaMode] = useState<ModularCtaMode>("use_cta");
  const [count, setCount] = useState(20);
  const [minimum, setMinimum] = useState(45);
  const [maximum, setMaximum] = useState(75);
  const [durationCustomized, setDurationCustomized] = useState(false);
  const [shortagePolicy, setShortagePolicy] = useState<IngredientShortagePolicy>("partial");
  const [run, setRun] = useState<ModularPlannerRun | null>(null);
  const [preview, setPreview] = useState<ModularPlannerItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [renderRun, setRenderRun] = useState<ModularRenderRun | null>(null);
  const [renderSelection, setRenderSelection] = useState<Set<string>>(new Set());
  const [renderBusy, setRenderBusy] = useState(false);
  const [renderError, setRenderError] = useState("");
  const [baseVideo, setBaseVideo] = useState<ModularRenderItem | null>(null);
  const [variantSelection, setVariantSelection] = useState<Set<string>>(new Set());
  const [variantProfile, setVariantProfile] = useState("active");
  const [variantRun, setVariantRun] = useState<ModularVariantPilotRun | null>(null);
  const [variantBusy, setVariantBusy] = useState(false);
  const [variantError, setVariantError] = useState("");
  const [workflow, setWorkflow] = useState<ModularProductionWorkflow>("automatic");
  const [productionProduct, setProductionProduct] = useState<ModularProductionProduct>("serum");
  const [productionCount, setProductionCount] = useState(20);
  const [productionSeed] = useState(() => crypto.randomUUID());
  const [productionJob, setProductionJob] = useState<ModularProductionJob | null>(null);
  const [productionBusy, setProductionBusy] = useState(false);
  const [productionError, setProductionError] = useState("");
  const videoRef = useRef<HTMLVideoElement>(null);
  const inventory = useApiQuery<ModularPlannerInventory>(
    `/api/modular-planner/inventory${query({ product })}`, 30_000, true
  );
  const recent = useApiQuery<{ runs: ModularPlannerRun[] }>("/api/modular-planner/runs?limit=20", 30_000, true);
  const eligibleVariants = useApiQuery<{ bases: ModularVariantEligibleBase[] }>(
    `/api/modular-variant-pilot/eligible${query({ planner_run_id: run?.planner_run_id ?? "" })}`, 10_000,
    Boolean(run?.planner_run_id && run.status === "approved")
  );
  const variantProfiles = useApiQuery<{ profiles: ModularVariantProfileRef[]; required_variant_count: number }>(
    "/api/modular-variant-pilot/profiles", 30_000, true
  );
  const productionProfiles = useApiQuery<{ profiles: ModularVariantProfileRef[] }>(
    "/api/modular-production/profiles", 30_000, true
  );
  const productionHistory = useApiQuery<{ jobs: ModularProductionJob[] }>(
    "/api/modular-production/jobs?limit=30", 10_000, true
  );

  useEffect(() => {
    if (!durationCustomized) {
      const [nextMinimum, nextMaximum] = suggested[`${template}:${ctaMode}`];
      setMinimum(nextMinimum);
      setMaximum(nextMaximum);
    }
  }, [template, ctaMode, durationCustomized]);

  useEffect(() => {
    if (!run && recent.envelope?.data.runs.length) setRun(recent.envelope.data.runs[0]);
  }, [recent.envelope?.data.runs, run]);

  useEffect(() => {
    if (!preview || !videoRef.current) return;
    videoRef.current.currentTime = preview.start_seconds;
    void videoRef.current.play().catch(() => undefined);
  }, [preview]);

  useEffect(() => {
    setRenderSelection(new Set()); setRenderRun(null); setBaseVideo(null); setRenderError("");
    setVariantSelection(new Set()); setVariantRun(null); setVariantError("");
    if (!run || run.status !== "approved") return;
    void getJson<{ runs: ModularRenderRun[] }>(`/api/modular-renderer/runs${query({ planner_run_id: run.planner_run_id, limit: 20 })}`)
      .then((response) => setRenderRun(response.data.runs[0] ?? null))
      .catch((caught) => setRenderError(caught instanceof Error ? caught.message : String(caught)));
  }, [run?.planner_run_id, run?.status]);

  useEffect(() => {
    if (!renderRun || !["queued", "waiting_for_production", "rendering"].includes(renderRun.status)) return;
    const timer = window.setInterval(() => {
      void getJson<ModularRenderRun>(`/api/modular-renderer/runs/${renderRun.render_run_id}`)
        .then((response) => setRenderRun(response.data))
        .catch((caught) => setRenderError(caught instanceof Error ? caught.message : String(caught)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [renderRun?.render_run_id, renderRun?.status]);

  useEffect(() => {
    if (!variantRun || !["queued", "waiting_for_production", "generating"].includes(variantRun.status)) return;
    const timer = window.setInterval(() => {
      void getJson<ModularVariantPilotRun>(`/api/modular-variant-pilot/runs/${variantRun.run_id}`)
        .then((response) => setVariantRun(response.data))
        .catch((caught) => setVariantError(caught instanceof Error ? caught.message : String(caught)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [variantRun?.run_id, variantRun?.status]);

  useEffect(() => {
    const activeJob = productionHistory.envelope?.data.jobs?.find((job) =>
      !["completed", "completed_with_failures", "failed", "cancelled"].includes(job.status)
    );
    if (!productionJob && activeJob) setProductionJob(activeJob);
  }, [productionHistory.envelope?.data.jobs, productionJob]);

  useEffect(() => {
    if (!productionJob || ["completed", "completed_with_failures", "failed", "cancelled"].includes(productionJob.status)) return;
    const timer = window.setInterval(() => {
      void getJson<ModularProductionJob>(`/api/modular-production/jobs/${productionJob.job_id}`)
        .then((response) => {
          setProductionJob(response.data);
          if (response.data.status === "awaiting_review" && response.data.planner_run_id) {
            void loadRun(response.data.planner_run_id);
          }
          if (["completed", "completed_with_failures", "failed", "cancelled"].includes(response.data.status)) {
            productionHistory.refresh();
          }
        })
        .catch((caught) => setProductionError(caught instanceof Error ? caught.message : String(caught)));
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [productionJob?.job_id, productionJob?.status]);

  const active = useMemo(
    () => run?.compositions.filter((composition) => composition.status === "draft" || composition.status === "approved") ?? [],
    [run]
  );
  const reuseNotices = useMemo(() => {
    const roles = template === "standard" ? ["hook", "benefits"]
      : template === "ingredient" ? ["hook", "ingredients", "benefits"]
        : ["hook", "benefits", "benefits"];
    if (ctaMode === "use_cta") roles.push("cta");
    const requirements = roles.reduce<Record<string, number>>((result, role) => {
      result[role] = (result[role] ?? 0) + count;
      return result;
    }, {});
    return Object.entries(requirements).flatMap(([role, uses]) => {
      const available = inventory.envelope?.data.roles[role]?.segments ?? 0;
      return available > 0 && uses > available
        ? [`${label(role)} inventory requires reuse: ${available} segments for ${uses} requested uses.`]
        : [];
    });
  }, [template, ctaMode, count, inventory.envelope?.data]);
  const productionAllocation = useMemo(
    () => productionProduct === "all_products"
      ? allocationFor(productionCount, productionSeed)
      : { [productionProduct]: productionCount },
    [productionProduct, productionCount, productionSeed]
  );
  const variantsPerBase = productionProfiles.envelope?.data.profiles?.find((profile) => profile.profile_id === variantProfile)?.variant_count ?? 0;

  function resetSuggested() {
    const [nextMinimum, nextMaximum] = suggested[`${template}:${ctaMode}`];
    setMinimum(nextMinimum); setMaximum(nextMaximum); setDurationCustomized(false);
  }

  async function generate() {
    setBusy(true); setError(""); setPreview(null);
    try {
      const response = await sendJson<ModularPlannerRun>("POST", "/api/modular-planner/runs", {
        production_method: "modular_video", product, requested_count: count,
        requested_template: template, cta_mode: ctaMode,
        target_min_duration: minimum, target_max_duration: maximum,
        ingredient_shortage_policy: shortagePolicy
      });
      setRun(response.data); recent.refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  async function loadRun(runId: string) {
    if (!runId) return;
    setBusy(true); setError(""); setPreview(null);
    try { setRun((await getJson<ModularPlannerRun>(`/api/modular-planner/runs/${runId}`)).data); }
    catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  async function mutate(path: string) {
    if (!run) return;
    setBusy(true); setError(""); setPreview(null);
    try {
      setRun((await sendJson<ModularPlannerRun>("POST", path, { expected_revision: run.revision })).data);
      recent.refresh();
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  }

  async function approve() {
    if (!run) return;
    if (run.shortfall > 0 && !window.confirm(`Approve ${run.generated_count} of ${run.requested_count} requested compositions?`)) return;
    await mutate(`/api/modular-planner/runs/${run.planner_run_id}/approve`);
  }

  function chooseFirstFive() {
    setRenderSelection(new Set(active.slice(0, 5).map((composition) => composition.composition_id)));
  }

  async function launchRender(manualRerender = false) {
    if (!run || run.status !== "approved" || renderSelection.size === 0) return;
    const verb = manualRerender ? "Rerender" : "Render";
    if (!window.confirm(`${verb} ${renderSelection.size} approved base video${renderSelection.size === 1 ? "" : "s"}?`)) return;
    setRenderBusy(true); setRenderError(""); setBaseVideo(null);
    try {
      const response = await sendJson<ModularRenderRun>("POST", "/api/modular-renderer/runs", {
        planner_run_id: run.planner_run_id,
        composition_ids: Array.from(renderSelection),
        manual_rerender: manualRerender
      });
      setRenderRun(response.data);
    } catch (caught) { setRenderError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setRenderBusy(false); }
  }

  function variantKey(base: Pick<ModularVariantEligibleBase, "render_run_id" | "composition_id">) {
    return `${base.render_run_id}:${base.composition_id}`;
  }

  async function launchVariantPilot(manualRerun = false) {
    const bases = (eligibleVariants.envelope?.data.bases ?? []).filter((base) => variantSelection.has(variantKey(base)));
    if (!bases.length) return;
    if (!window.confirm(`${bases.length} base videos × 6 variants = ${bases.length * 6} outputs. Generate pilot variants?`)) return;
    setVariantBusy(true); setVariantError("");
    try {
      const response = await sendJson<ModularVariantPilotRun>("POST", "/api/modular-variant-pilot/runs", {
        bases: bases.map(({ render_run_id, composition_id }) => ({ render_run_id, composition_id })),
        profile_id: variantProfile, manual_rerun: manualRerun
      });
      setVariantRun(response.data);
    } catch (caught) { setVariantError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setVariantBusy(false); }
  }

  async function startProduction(explicitRerun = false) {
    setProductionBusy(true); setProductionError("");
    try {
      const response = await sendJson<ModularProductionJob>("POST", "/api/modular-production/jobs", {
        production_method: "modular_video", workflow_mode: workflow, product: productionProduct,
        requested_base_count: productionCount, requested_template: template, cta_mode: ctaMode,
        target_min_duration: minimum, target_max_duration: maximum,
        ingredient_shortage_policy: shortagePolicy, variant_profile_id: variantProfile,
        seed: productionSeed,
        explicit_rerun: explicitRerun
      });
      setProductionJob(response.data); productionHistory.refresh();
    } catch (caught) { setProductionError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setProductionBusy(false); }
  }

  async function continueProduction() {
    if (!productionJob) return;
    setProductionBusy(true); setProductionError("");
    try {
      const response = await sendJson<ModularProductionJob>(
        "POST", `/api/modular-production/jobs/${productionJob.job_id}/continue`,
        {
          expected_planner_revision: run?.revision,
          expected_planner_revisions: Object.fromEntries(productionJob.product_plans.map((plan) => [plan.product, plan.revision]))
        }
      );
      setProductionJob(response.data);
    } catch (caught) { setProductionError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setProductionBusy(false); }
  }

  async function cancelProduction() {
    if (!productionJob) return;
    setProductionBusy(true); setProductionError("");
    try {
      setProductionJob((await sendJson<ModularProductionJob>(
        "POST", `/api/modular-production/jobs/${productionJob.job_id}/cancel`
      )).data);
    } catch (caught) { setProductionError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setProductionBusy(false); }
  }

  function stopAtEnd() {
    if (preview && videoRef.current && videoRef.current.currentTime >= preview.end_seconds) videoRef.current.pause();
  }

  return (
    <section className="page-stack modular-planner-page">
      <div className="page-heading">
        <div><span className="eyebrow">Production method</span><h1>Modular Video</h1><p>Planner → immutable compositions → bases → Variants → compliance → scoring → final output.</p></div>
        <button className="secondary-button" onClick={() => { inventory.refresh(); recent.refresh(); }}><RefreshCw size={16} /> Refresh</button>
      </div>

      <article className="panel modular-production-new">
        <div className="panel-head"><div><h2>New Modular Production</h2><p>Automatic is the default. Review First pauses on the existing planner workspace before rendering.</p></div></div>
        <div className="modular-planner-fields">
          <label><span>Production product</span><select aria-label="Production product" value={productionProduct} onChange={(event) => { const next = event.target.value as ModularProductionProduct; setProductionProduct(next); if (next === "all_products") setProductionCount((current) => Math.max(6, current)); }}>{productionProducts.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label><span>Base videos</span><input aria-label="Production base videos" type="number" min={productionProduct === "all_products" ? 6 : 1} max={100} value={productionCount} onChange={(event) => setProductionCount(Number(event.target.value))} /></label>
          <label><span>Production template</span><select aria-label="Production template" value={template} onChange={(event) => setTemplate(event.target.value as ModularTemplate)}>{templates.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label><span>Production CTA</span><select aria-label="Production CTA" value={ctaMode} onChange={(event) => setCtaMode(event.target.value as ModularCtaMode)}><option value="use_cta">Use CTA</option><option value="no_cta">No CTA</option></select></label>
          <label><span>Production workflow</span><select aria-label="Production workflow" value={workflow} onChange={(event) => setWorkflow(event.target.value as ModularProductionWorkflow)}><option value="automatic">Automatic</option><option value="review_first">Review First</option></select></label>
          <label><span>Production variants profile</span><select aria-label="Production variants profile" value={variantProfile} onChange={(event) => setVariantProfile(event.target.value)}>{productionProfiles.envelope?.data.profiles?.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name} · {profile.variant_count}</option>)}</select></label>
          <label><span>Production minimum</span><input aria-label="Production minimum seconds" type="number" min={15} max={180} value={minimum} onChange={(event) => { setMinimum(Number(event.target.value)); setDurationCustomized(true); }} /></label>
          <label><span>Production maximum</span><input aria-label="Production maximum seconds" type="number" min={15} max={180} value={maximum} onChange={(event) => { setMaximum(Number(event.target.value)); setDurationCustomized(true); }} /></label>
          {template === "ingredient" && <label className="modular-fallback"><input type="checkbox" checked={shortagePolicy === "fallback_to_standard"} onChange={(event) => setShortagePolicy(event.target.checked ? "fallback_to_standard" : "partial")} /> Ingredient fallback to Standard</label>}
        </div>
        {productionProduct === "all_products" && <div className="modular-all-products-preview"><p>The total base-video count will be distributed as evenly as possible across all 6 products. Each video still contains only one product.</p><div>{products.map(({ value, label: productLabel }) => <span key={value}><strong>{productLabel}</strong> {productionAllocation[value]} bases → {(productionAllocation[value] ?? 0) * variantsPerBase} variants</span>)}</div></div>}
        <div className="modular-production-launch"><strong>{productionCount} base videos → {productionCount * variantsPerBase} expected final variants</strong><button className="primary-button" disabled={productionBusy || minimum >= maximum || productionCount < (productionProduct === "all_products" ? 6 : 1) || productionCount > 100} onClick={() => void startProduction(false)}><Play size={16} /> {productionBusy ? "Starting…" : "Start Modular Production"}</button></div>
        {productionError && <div className="modular-error" role="alert">{productionError}</div>}
      </article>

      {productionJob && <article className="panel modular-production-current">
        <div className="panel-head"><div><span className={`modular-run-status ${productionJob.status}`}>{label(productionJob.status)}</span><h2>Current Production Job</h2><p>{label(productionJob.product)} · {label(productionJob.workflow_mode)} · {Math.round(productionJob.stage_progress)}% current stage</p></div><select aria-label="Modular production history" value={productionJob.job_id} onChange={(event) => { const selected = productionHistory.envelope?.data.jobs?.find((job) => job.job_id === event.target.value); if (selected) setProductionJob(selected); }}>{productionHistory.envelope?.data.jobs?.map((job) => <option key={job.job_id} value={job.job_id}>{new Date(job.created_at).toLocaleString()} · {label(job.product)} · {label(job.status)}</option>)}</select></div>
        <div className="modular-production-progress"><i style={{ width: `${Math.max(0, Math.min(100, productionJob.stage_progress))}%` }} /></div>
        <div className="modular-production-metrics">
          <div><span>Requested bases</span><strong>{productionJob.requested_base_count}</strong></div><div><span>Generated</span><strong>{productionJob.generated_base_count}</strong></div><div><span>Rendered</span><strong>{productionJob.rendered_base_count}</strong></div><div><span>Expected variants</span><strong>{productionJob.expected_variant_count}</strong></div><div><span>Generated variants</span><strong>{productionJob.generated_variant_count}</strong></div><div><span>Compliance passed</span><strong>{productionJob.compliance_passed_count}</strong></div><div><span>Scored</span><strong>{productionJob.scored_count}</strong></div><div><span>Exported</span><strong>{productionJob.exported_count}</strong></div>
        </div>
        {productionJob.product_scope === "all" && <div className="modular-product-progress">{products.map(({ value, label: productLabel }) => { const flow = productionJob.product_subflows[value]; return flow ? <section key={value}><h3>{productLabel}</h3><span>{flow.generated_base_count}/{flow.requested_base_count} planned</span><span>{flow.rendered_base_count}/{flow.generated_base_count} bases rendered</span><span>{flow.generated_variant_count}/{flow.rendered_base_count * productionJob.variants_per_base} variants</span>{flow.generated_base_count < flow.requested_base_count && <strong className="modular-shortfall">Shortfall: {flow.requested_base_count - flow.generated_base_count}</strong>}</section> : null; })}</div>}
        {productionJob.status === "awaiting_review" && productionJob.product_scope === "all" && <div className="modular-product-plans">{productionJob.product_plans.map((plan) => <section key={plan.product}><h3>{label(plan.product)} — {plan.generated_count} planned</h3><div>{plan.compositions?.filter((composition) => ["draft", "approved"].includes(composition.status)).map((composition) => <button type="button" className="tiny-button" key={composition.composition_id} onClick={() => void loadRun(plan.planner_run_id)}>Composition {composition.ordinal}</button>)}</div>{plan.shortfall > 0 && <p className="modular-shortfall">Shortfall: {plan.shortfall}</p>}</section>)}</div>}
        {(productionJob.failed_base_count + productionJob.failed_variant_count + productionJob.compliance_rejected_count + productionJob.export_failed_count) > 0 && <div className="modular-warnings"><p>{productionJob.failed_base_count} base failures · {productionJob.failed_variant_count} variant failures · {productionJob.compliance_rejected_count} compliance rejections · {productionJob.export_failed_count} export failures</p></div>}
        {productionJob.error_message && <div className="modular-error">{productionJob.error_message}</div>}
        <div className="modular-production-actions">
          {productionJob.status === "awaiting_review" && <button className="primary-button" disabled={productionBusy || productionJob.product_plans.length === 0} onClick={() => void continueProduction()}><CheckCircle2 size={16} /> {productionJob.product_scope === "all" ? "Approve All Products" : "Approve & Continue Production"}</button>}
          {!["completed", "completed_with_failures", "failed", "cancelled"].includes(productionJob.status) && <button className="secondary-button" disabled={productionBusy || productionJob.cancel_requested} onClick={() => void cancelProduction()}>Stop after current operation</button>}
          {["completed", "completed_with_failures", "failed", "cancelled"].includes(productionJob.status) && <button className="secondary-button" disabled={productionBusy} onClick={() => void startProduction(true)}>Explicit rerun</button>}
        </div>
      </article>}

      <div className="modular-advanced-heading"><span className="eyebrow">Advanced / Debug</span><h2>Planner, manual renderer, and pilot history</h2><p>These validated tools remain available for review and diagnostics.</p></div>

      <article className="panel modular-planner-settings">
        <div className="panel-head"><div><h2>Planner settings</h2><p>One product per composition.</p></div></div>
        <div className="modular-planner-fields">
          <label><span>Product</span><select aria-label="Product" value={product} onChange={(event) => setProduct(event.target.value as ModularProduct)}>{products.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label><span>Base compositions</span><input aria-label="Base compositions" type="number" min={1} max={100} value={count} onChange={(event) => setCount(Number(event.target.value))} /></label>
          <label><span>Template</span><select aria-label="Template" value={template} onChange={(event) => setTemplate(event.target.value as ModularTemplate)}>{templates.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
          <label><span>CTA mode</span><select aria-label="CTA mode" value={ctaMode} onChange={(event) => setCtaMode(event.target.value as ModularCtaMode)}><option value="use_cta">Use CTA</option><option value="no_cta">No CTA</option></select></label>
          <label><span>Minimum seconds</span><input aria-label="Minimum seconds" type="number" min={15} max={180} value={minimum} onChange={(event) => { setMinimum(Number(event.target.value)); setDurationCustomized(true); }} /></label>
          <label><span>Maximum seconds</span><input aria-label="Maximum seconds" type="number" min={15} max={180} value={maximum} onChange={(event) => { setMaximum(Number(event.target.value)); setDurationCustomized(true); }} /></label>
        </div>
        <div className="modular-planner-setting-actions">
          <button className="tiny-button" type="button" onClick={resetSuggested}>Reset to suggested</button>
          {template === "ingredient" && <label className="modular-fallback"><input type="checkbox" checked={shortagePolicy === "fallback_to_standard"} onChange={(event) => setShortagePolicy(event.target.checked ? "fallback_to_standard" : "partial")} /> Fill Ingredient shortfall with Standard</label>}
        </div>
        {inventory.envelope?.data && <div className="modular-inventory" aria-label="Inventory readiness">{Object.entries(inventory.envelope.data.roles).map(([role, value]) => <div key={role}><span>{label(role)}</span><strong>{value.segments}</strong><small>{value.distinct_sources} VODs</small></div>)}</div>}
        {reuseNotices.length > 0 && <div className="modular-warnings modular-preflight-warnings">{reuseNotices.map((notice) => <p key={notice}>{notice}</p>)}</div>}
        <div className="modular-generate-row"><span>{minimum}–{maximum} sec · {label(template)} · {ctaMode === "use_cta" ? "CTA" : "No CTA"}</span><button className="primary-button" disabled={busy || minimum >= maximum || count < 1 || count > 100} onClick={() => void generate()}><Play size={16} />{busy ? "Planning…" : "Generate Plans"}</button></div>
        {error && <div className="modular-error" role="alert">{error}</div>}
      </article>

      <article className="panel modular-run-picker">
        <label><span>Saved planner run</span><select aria-label="Saved planner run" value={run?.planner_run_id ?? ""} onChange={(event) => void loadRun(event.target.value)}><option value="">Select a run</option>{recent.envelope?.data.runs.map((item) => <option value={item.planner_run_id} key={item.planner_run_id}>{item.product} · {item.requested_template} · {item.status} · {item.generated_count}/{item.requested_count}</option>)}</select></label>
      </article>

      {run && <>
        <article className="panel modular-run-summary">
          <div><span className={`modular-run-status ${run.status}`}>{run.status}</span><h2>{label(run.product)} · {label(run.requested_template)}</h2><p>{run.target_min_duration}–{run.target_max_duration} sec · {run.cta_mode === "use_cta" ? "Use CTA" : "No CTA"} · seed {run.seed.slice(0, 12)}</p></div>
          <div className="modular-counts"><div><span>Requested</span><strong>{run.requested_count}</strong></div><div><span>Generated</span><strong>{run.generated_count}</strong></div><div><span>Shortfall</span><strong>{run.shortfall}</strong></div></div>
          {run.warnings.length > 0 && <div className="modular-warnings">{run.warnings.map((warning, index) => <p key={`${warning.code}-${index}`}>{warningText(warning)}</p>)}</div>}
          {run.status === "draft" && <button className="primary-button modular-approve" disabled={busy || active.length === 0} onClick={() => void approve()}><CheckCircle2 size={16} /> Approve {active.length} composition{active.length === 1 ? "" : "s"}</button>}
          {run.status === "approved" && <p className="modular-approved-note">Approved manifest is immutable and ready for pilot rendering.</p>}
        </article>

        {run.status === "approved" && <article className="panel modular-render-pilot">
          <div className="panel-head"><div><h2>Render Pilot</h2><p>Create raw joined base MP4s from selected approved compositions. Variants will not run.</p></div><span className={`modular-render-status ${renderRun?.status ?? "idle"}`}>{label(renderRun?.status ?? "idle")}</span></div>
          <div className="modular-render-actions">
            <span>{renderSelection.size} selected</span>
            <button className="tiny-button" onClick={chooseFirstFive}>Select first 5</button>
            <button className="tiny-button" onClick={() => setRenderSelection(new Set(active.map((composition) => composition.composition_id)))}>Select all</button>
            <button className="tiny-button" onClick={() => setRenderSelection(new Set())}>Clear</button>
            <button className="primary-button" disabled={renderBusy || renderSelection.size === 0} onClick={() => void launchRender(false)}><Film size={16} /> {renderBusy ? "Launching…" : "Render selected"}</button>
            {renderRun && ["completed", "partial_failure", "failed"].includes(renderRun.status) && <button className="secondary-button" disabled={renderBusy || renderSelection.size === 0} onClick={() => void launchRender(true)}>Rerender selected</button>}
          </div>
          {renderRun && <div className="modular-render-summary">
            <div><span>Requested</span><strong>{renderRun.requested_count}</strong></div><div><span>Completed</span><strong>{renderRun.succeeded_count}</strong></div><div><span>Failed</span><strong>{renderRun.failed_count}</strong></div>
            {renderRun.current_composition_id && <p>Current composition: {renderRun.current_composition_id}</p>}
          </div>}
          {renderError && <div className="modular-error" role="alert">{renderError}</div>}
          {renderRun && <div className="modular-render-items">{renderRun.items.map((item) => <div key={item.composition_id} className={`modular-render-item ${item.status}`}>
            <div><strong>Composition {item.ordinal} · {label(item.product)}</strong><span>{label(item.template)} · {label(item.status)}</span></div>
            <div><span>Expected {item.expected_duration.toFixed(2)} sec</span><span>{item.rendered_duration == null ? "Actual —" : `Actual ${item.rendered_duration.toFixed(2)} sec`}</span></div>
            {item.error_message && <p>{item.error_message}</p>}
            {item.status === "completed" && <button className="tiny-button" onClick={() => setBaseVideo(item)}><Play size={13} /> Play Base Video</button>}
          </div>)}</div>}
          {baseVideo && renderRun && <div className="modular-base-player"><h3>Joined base video · Composition {baseVideo.ordinal}</h3><video controls preload="metadata" src={`/api/modular-renderer/runs/${renderRun.render_run_id}/media/${baseVideo.composition_id}`} /><p>{label(baseVideo.product)} · {label(baseVideo.template)} · {baseVideo.expected_duration.toFixed(2)} sec expected · {baseVideo.rendered_duration?.toFixed(2)} sec actual</p></div>}
        </article>}

        {run.status === "approved" && <article className="panel modular-variant-pilot">
          <div className="panel-head"><div><h2>Generate Variants Pilot</h2><p>Use completed modular bases as ordinary inputs to the existing six-variant profile. No scoring, compliance, export, or delivery.</p></div><span className={`modular-render-status ${variantRun?.status ?? "idle"}`}>{label(variantRun?.status ?? "idle")}</span></div>
          <div className="modular-render-actions">
            <span>{variantSelection.size} selected · {variantSelection.size * 6} outputs</span>
            <button className="tiny-button" onClick={() => setVariantSelection(new Set((eligibleVariants.envelope?.data.bases ?? []).slice(0, 2).map(variantKey)))}>Select first 2</button>
            <button className="tiny-button" onClick={() => setVariantSelection(new Set((eligibleVariants.envelope?.data.bases ?? []).map(variantKey)))}>Select all</button>
            <button className="tiny-button" onClick={() => setVariantSelection(new Set())}>Clear</button>
            <label className="modular-variant-profile"><span>Variant profile</span><select aria-label="Variant profile" value={variantProfile} onChange={(event) => setVariantProfile(event.target.value)}>{variantProfiles.envelope?.data.profiles?.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.name} · {profile.variant_count} variants</option>)}</select></label>
            <button className="primary-button" disabled={variantBusy || variantSelection.size === 0} onClick={() => void launchVariantPilot(false)}><Film size={16} /> {variantBusy ? "Launching…" : "Generate variants"}</button>
            {variantRun && ["completed", "partial_failure", "failed"].includes(variantRun.status) && <button className="secondary-button" disabled={variantBusy || variantSelection.size === 0} onClick={() => void launchVariantPilot(true)}>Explicit rerun</button>}
          </div>
          {variantError && <div className="modular-error" role="alert">{variantError}</div>}
          <div className="modular-variant-bases">{(eligibleVariants.envelope?.data.bases ?? []).map((base) => <label key={variantKey(base)} className="modular-variant-base"><input aria-label={`Select completed base ${base.ordinal} for variants`} type="checkbox" checked={variantSelection.has(variantKey(base))} onChange={(event) => setVariantSelection((prior) => { const next = new Set(prior); if (event.target.checked) next.add(variantKey(base)); else next.delete(variantKey(base)); return next; })} /><span><strong>Composition {base.ordinal} · {label(base.product)}</strong><small>{base.rendered_duration?.toFixed(2) ?? "—"} sec · {base.renderer_version}</small></span></label>)}</div>
          {variantRun && <div className="modular-render-summary"><div><span>Bases</span><strong>{variantRun.succeeded_base_count}/{variantRun.requested_base_count}</strong></div><div><span>Outputs</span><strong>{variantRun.total_completed_outputs}/{variantRun.total_expected_outputs}</strong></div><div><span>Failed</span><strong>{variantRun.failed_base_count}</strong></div>{variantRun.current_render_item_id && <p>Current base: {variantRun.current_render_item_id}</p>}</div>}
          {variantRun && <div className="modular-variant-results">{variantRun.items.map((item) => <details key={item.modular_render_item_id} className={`modular-variant-result ${item.status}`} open={item.status === "completed"}><summary><strong>Composition {item.ordinal} · {label(item.product)}</strong><span>{label(item.status)} · {item.produced_variant_count}/{item.expected_variant_count} variants{item.generation_seconds ? ` · ${item.generation_seconds.toFixed(1)} sec` : ""}</span></summary>{item.error && <p className="modular-error">{item.error}</p>}<div className="modular-variant-grid"><div><video controls preload="metadata" src={`/api/modular-renderer/runs/${item.render_run_id}/media/${item.composition_id}`} /><strong>Raw base</strong></div>{item.outputs.map((output) => <div key={output.media_id}><video controls preload="metadata" src={output.url} /><strong>var{output.variant_index} · {output.variant_name}</strong><small>{output.duration.toFixed(2)} sec · {output.width}×{output.height} · {(output.file_size / 1_048_576).toFixed(1)} MB</small></div>)}</div></details>)}</div>}
        </article>}

        <div className="modular-workspace">
          <div className="modular-compositions">
            {active.map((composition) => <CompositionCard key={composition.composition_id} composition={composition} immutable={run.status === "approved" || busy} selected={renderSelection.has(composition.composition_id)} onSelect={run.status === "approved" ? (selected) => setRenderSelection((prior) => { const next = new Set(prior); if (selected) next.add(composition.composition_id); else next.delete(composition.composition_id); return next; }) : undefined} onPreview={setPreview} onRegenerate={() => void mutate(`/api/modular-planner/runs/${run.planner_run_id}/compositions/${composition.composition_id}/regenerate`)} onRemove={() => void mutate(`/api/modular-planner/runs/${run.planner_run_id}/compositions/${composition.composition_id}/remove`)} />)}
          </div>
          <aside className="panel modular-preview">
            <div className="panel-head"><div><h2>Source preview</h2><p>Selected source range only.</p></div></div>
            {preview ? <>
              <video ref={videoRef} controls preload="metadata" src={`/api/modular-scanner/media/${preview.source_id}`} onLoadedMetadata={() => { if (videoRef.current) videoRef.current.currentTime = preview.start_seconds; }} onTimeUpdate={stopAtEnd} />
              <strong>{label(preview.role)} · {preview.source_filename}</strong><span>{time(preview.start_seconds)}–{time(preview.end_seconds)}</span><p>{preview.transcript_text}</p>
              <button className="secondary-button" onClick={() => { if (videoRef.current) { videoRef.current.currentTime = preview.start_seconds; void videoRef.current.play(); } }}><RotateCcw size={15} /> Replay range</button>
            </> : <p className="muted">Choose Preview on a composition item.</p>}
          </aside>
        </div>
      </>}
    </section>
  );
}

function CompositionCard({ composition, immutable, selected, onSelect, onPreview, onRegenerate, onRemove }: { composition: ModularPlannerComposition; immutable: boolean; selected: boolean; onSelect?: (selected: boolean) => void; onPreview: (item: ModularPlannerItem) => void; onRegenerate: () => void; onRemove: () => void }) {
  const continuity = composition.selection_metadata?.hook_benefits_continuity;
  return <article className="panel modular-composition-card">
    <div className="modular-composition-head"><div>{onSelect && <label className="modular-render-checkbox"><input aria-label={`Select composition ${composition.ordinal} for rendering`} type="checkbox" checked={selected} onChange={(event) => onSelect(event.target.checked)} /> Render</label>}<span>Composition {composition.ordinal}</span><h3>{label(composition.actual_template)} · {composition.actual_duration.toFixed(1)} sec</h3><small>{composition.distinct_source_count} distinct VODs · score {composition.selection_score.toFixed(1)}{continuity !== undefined ? ` · continuity ${Math.round(continuity * 100)}%` : ""}</small>{composition.fallback_reason && <em>Fallback: {label(composition.fallback_reason)}</em>}</div>{!immutable && <div><button className="tiny-button" onClick={onRegenerate}><RefreshCw size={13} /> Regenerate</button><button className="tiny-button danger" onClick={onRemove}><Trash2 size={13} /> Remove</button></div>}</div>
    <div className="modular-item-list">{composition.items.map((item) => { const boundary = item.ranking_metadata?.joinability?.boundary_label; return <div className="modular-item" key={`${composition.composition_id}-${item.position}`}><div><strong>{label(item.role)}</strong><span>{item.source_filename}</span></div><div><span>{time(item.start_seconds)}–{time(item.end_seconds)} · {item.duration_seconds.toFixed(1)} sec</span><small>Confidence {(item.confidence * 100).toFixed(0)}%{boundary ? ` · Boundary: ${boundary}` : ""}</small></div><p>{item.transcript_text}</p><button className="tiny-button" onClick={() => onPreview(item)}>Preview</button></div>; })}</div>
  </article>;
}
