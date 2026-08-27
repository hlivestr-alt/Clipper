import { useEffect, useMemo, useRef, useState } from "react";
import { Library, ListChecks, Play, RefreshCw, RotateCcw, Search, Video } from "lucide-react";

import { getJson, query, sendJson } from "../api";
import { useApiQuery } from "../useApiQuery";
import "./modularScanner.css";

export type ModularScan = {
  scan_id: string;
  source_id: string;
  generation: number;
  trigger: "scan" | "rescan";
  status: string;
  progress_current: number;
  progress_total: number;
  accepted_count: number;
  rejected_count: number;
  error?: string | null;
  is_current: boolean;
};

export type ModularSource = {
  source_id: string;
  filename: string;
  file_size: number;
  mtime_ns: number;
  duration_seconds?: number | null;
  current_scan?: ModularScan | null;
  active_scan?: ModularScan | null;
};

export type ModularSegment = {
  segment_id: string;
  scan_id: string;
  source_id: string;
  vod_filename: string;
  product: string;
  role: string;
  start_seconds: number;
  end_seconds: number;
  duration_seconds: number;
  confidence: number;
  transcript_text: string;
  reason: string;
};

type SourcesPayload = { sources: ModularSource[] };
type HistoryPayload = { scans: ModularScan[] };
type SegmentsPayload = { segments: ModularSegment[] };
type ScanPayload = { scan: ModularScan; reused: boolean };
type BatchPreview = {
  total_eligible: number;
  already_current: number;
  already_active: number;
  would_queue: number;
  needs_check: number;
  will_evaluate: number;
};
type BatchStatus = {
  batch_id: string;
  status: string;
  total_eligible: number;
  discovered: number;
  checked: number;
  checking: number;
  already_current: number;
  already_active: number;
  queued: number;
  completed: number;
  failed: number;
  remaining: number;
  currently_running?: { source_id: string; filename: string; status: string } | null;
};
type BatchStartPayload = {
  launched: boolean;
  reused?: boolean;
  batch: BatchStatus | null;
  total_eligible?: number;
  already_current?: number;
  already_active?: number;
  would_queue?: number;
};

const products = ["cleanser", "toner", "serum", "eye_cream", "mask", "skin_cream"];
const roles = ["hook", "benefits", "ingredients", "cta"];

function formatTime(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "–";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  return `${minutes}:${remainder.toFixed(3).padStart(6, "0")}`;
}

function formatStatus(value?: string | null): string {
  return String(value || "not scanned").replace(/_/g, " ");
}

function scanIsBusy(scan?: ModularScan | null): boolean {
  return Boolean(scan && !["completed", "failed"].includes(scan.status));
}

export function ModularScannerPage() {
  const sourcesQuery = useApiQuery<SourcesPayload>("/api/modular-scanner/sources", 5_000, true);
  const sources = sourcesQuery.envelope?.data.sources ?? [];
  const [sourceId, setSourceId] = useState("");
  const selectedSource = sources.find((source) => source.source_id === sourceId) ?? null;
  const [watchedScanId, setWatchedScanId] = useState("");
  const watchedScan = useApiQuery<{ scan: ModularScan }>(
    watchedScanId ? `/api/modular-scanner/scans/${watchedScanId}` : "",
    (data) => scanIsBusy(data?.scan) ? 1_500 : false,
    Boolean(watchedScanId)
  );
  const history = useApiQuery<HistoryPayload>(
    sourceId ? `/api/modular-scanner/scans${query({ source_id: sourceId })}` : "",
    4_000,
    Boolean(sourceId)
  );
  const [historyScanId, setHistoryScanId] = useState("");
  const [product, setProduct] = useState("");
  const [role, setRole] = useState("");
  const [minimumConfidence, setMinimumConfidence] = useState("0");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState("timestamp");
  const segmentPath = sourceId
    ? `/api/modular-scanner/segments${query({
        source_id: sourceId,
        scan_id: historyScanId || undefined,
        product: product || undefined,
        role: role || undefined,
        minimum_confidence: minimumConfidence,
        search: search || undefined,
        sort
      })}`
    : "";
  const segmentsQuery = useApiQuery<SegmentsPayload>(segmentPath, 4_000, Boolean(sourceId));
  const segments = segmentsQuery.envelope?.data.segments ?? [];
  const [preview, setPreview] = useState<ModularSegment | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [actionError, setActionError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [batchPreview, setBatchPreview] = useState<BatchPreview | null>(null);
  const [batchSubmitting, setBatchSubmitting] = useState(false);
  const [batchId, setBatchId] = useState("");
  const [batchSnapshot, setBatchSnapshot] = useState<BatchStatus | null>(null);
  const [batchNotice, setBatchNotice] = useState("");
  const batchQuery = useApiQuery<{ batch: BatchStatus }>(
    batchId ? `/api/modular-scanner/batches/${batchId}` : "",
    (data) => ["preparing", "running"].includes(data?.batch.status ?? "") ? 1_500 : false,
    Boolean(batchId)
  );
  const batchStatus = batchQuery.envelope?.data.batch ?? batchSnapshot;
  const batchActive = Boolean(batchStatus && ["preparing", "running"].includes(batchStatus.status));

  useEffect(() => {
    if (!sourceId && sources.length) setSourceId(sources[0].source_id);
  }, [sourceId, sources]);

  useEffect(() => {
    setHistoryScanId("");
    setPreview(null);
  }, [sourceId]);

  useEffect(() => {
    const status = watchedScan.envelope?.data.scan.status;
    if (status === "completed" || status === "failed") {
      sourcesQuery.refresh();
      history.refresh();
      segmentsQuery.refresh();
    }
  }, [watchedScan.envelope?.data.scan.status]);

  useEffect(() => {
    if (batchStatus && ["completed", "completed_with_failures", "failed"].includes(batchStatus.status)) {
      sourcesQuery.refresh();
      history.refresh();
      segmentsQuery.refresh();
    }
  }, [batchStatus?.status]);

  useEffect(() => {
    if (!preview || !videoRef.current) return;
    videoRef.current.currentTime = preview.start_seconds;
    void videoRef.current.play().catch(() => undefined);
  }, [preview]);

  const displayedStatus = watchedScan.envelope?.data.scan
    ?? selectedSource?.active_scan
    ?? selectedSource?.current_scan
    ?? null;
  const busy = submitting || scanIsBusy(displayedStatus);
  const historyRows = history.envelope?.data.scans ?? [];
  const selectedHistory = useMemo(
    () => historyRows.find((scan) => scan.scan_id === historyScanId) ?? null,
    [historyRows, historyScanId]
  );

  async function start(rescan: boolean) {
    if (!sourceId) return;
    setSubmitting(true);
    setActionError("");
    try {
      const result = rescan
        ? await sendJson<ScanPayload>("POST", `/api/modular-scanner/sources/${sourceId}/rescan`)
        : await sendJson<ScanPayload>("POST", "/api/modular-scanner/scans", { source_id: sourceId });
      setWatchedScanId(result.data.scan.scan_id);
      history.refresh();
      sourcesQuery.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function previewBatch() {
    setBatchSubmitting(true);
    setActionError("");
    setBatchNotice("");
    try {
      const result = await getJson<BatchPreview>("/api/modular-scanner/batch-scan-preview");
      setBatchPreview(result.data);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBatchSubmitting(false);
    }
  }

  async function launchBatch() {
    setBatchSubmitting(true);
    setActionError("");
    try {
      const result = await sendJson<BatchStartPayload>("POST", "/api/modular-scanner/batches");
      setBatchPreview(null);
      if (!result.data.launched || !result.data.batch) {
        setBatchNotice("All eligible VODs already have compatible scans.");
        return;
      }
      setBatchSnapshot(result.data.batch);
      setBatchId(result.data.batch.batch_id);
      setBatchNotice(result.data.reused ? "Continuing the active batch." : "Preparing batch...");
      sourcesQuery.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setBatchSubmitting(false);
    }
  }

  function replay() {
    if (!preview || !videoRef.current) return;
    videoRef.current.currentTime = preview.start_seconds;
    void videoRef.current.play().catch(() => undefined);
  }

  function stopAtEnd() {
    if (preview && videoRef.current && videoRef.current.currentTime >= preview.end_seconds) {
      videoRef.current.pause();
    }
  }

  return (
    <section className="page-stack modscan-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">Standalone workspace</span>
          <h1>Modular Scanner</h1>
          <p>Find reusable product moments and preview their source ranges without creating clips.</p>
        </div>
        <button className="secondary-button" onClick={() => sourcesQuery.refresh()} aria-label="Refresh sources">
          <RefreshCw size={16} /> Refresh
        </button>
      </div>

      <article className="panel modscan-source-panel">
        <div className="modscan-source-fields">
          <label>
            <span>Source VOD</span>
            <select aria-label="Source VOD" value={sourceId} onChange={(event) => setSourceId(event.target.value)}>
              <option value="">Select a VOD</option>
              {sources.map((source) => <option key={source.source_id} value={source.source_id}>{source.filename}</option>)}
            </select>
          </label>
          <div className="modscan-source-meta">
            <strong>{selectedSource?.filename ?? "No VOD selected"}</strong>
            <span>Duration {formatTime(selectedSource?.duration_seconds)}</span>
            <span className={`modscan-status status-${displayedStatus?.status ?? "idle"}`}>{formatStatus(displayedStatus?.status)}</span>
          </div>
        </div>
        <div className="modscan-actions">
          <button className="primary-button" disabled={!sourceId || busy} onClick={() => void start(false)}>
            <Search size={16} /> Scan VOD
          </button>
          <button className="secondary-button" disabled={!sourceId || busy || !selectedSource?.current_scan} onClick={() => void start(true)}>
            <RotateCcw size={16} /> Rescan
          </button>
          <button className="secondary-button modscan-batch-button" disabled={batchSubmitting || batchActive} onClick={() => void previewBatch()}>
            <ListChecks size={16} /> Scan All Unscanned VODs
          </button>
        </div>
        {batchPreview && (
          <div className="modscan-batch-confirm" role="dialog" aria-label="Batch scan confirmation">
            <div>
              <h3>Scan All Unscanned VODs</h3>
              <p>{batchPreview.total_eligible} VODs found</p>
            </div>
            <dl>
              <div><dt>Already current</dt><dd>{batchPreview.already_current}</dd></div>
              <div><dt>Already active</dt><dd>{batchPreview.already_active}</dd></div>
              <div><dt>Will be queued</dt><dd>{batchPreview.would_queue}</dd></div>
              <div><dt>Needs evaluation</dt><dd>{batchPreview.needs_check}</dd></div>
            </dl>
            {batchPreview.will_evaluate === 0 && <p>All eligible VODs already have compatible scans.</p>}
            <div className="modscan-batch-confirm-actions">
              <button className="secondary-button" onClick={() => setBatchPreview(null)}>
                {batchPreview.will_evaluate ? "Cancel" : "Close"}
              </button>
              {batchPreview.will_evaluate > 0 && (
                <button className="primary-button" disabled={batchSubmitting} onClick={() => void launchBatch()}>
                  Evaluate {batchPreview.will_evaluate} VODs
                </button>
              )}
            </div>
          </div>
        )}
        {batchStatus && (
          <div className="modscan-batch-progress" aria-label="Batch scan progress">
            <div className="modscan-batch-progress-heading"><strong>Batch Scan</strong><span>{formatStatus(batchStatus.status)}</span></div>
            <dl>
              <div><dt>Total</dt><dd>{batchStatus.total_eligible}</dd></div>
              <div><dt>Discovered</dt><dd>{batchStatus.discovered}</dd></div>
              <div><dt>Checked</dt><dd>{batchStatus.checked}</dd></div>
              <div><dt>Checking</dt><dd>{batchStatus.checking}</dd></div>
              <div><dt>Already current</dt><dd>{batchStatus.already_current}</dd></div>
              <div><dt>Already active</dt><dd>{batchStatus.already_active}</dd></div>
              <div><dt>Queued</dt><dd>{batchStatus.queued}</dd></div>
              <div><dt>Completed</dt><dd>{batchStatus.completed}</dd></div>
              <div><dt>Failed</dt><dd>{batchStatus.failed}</dd></div>
              <div><dt>Remaining</dt><dd>{batchStatus.remaining}</dd></div>
            </dl>
            {batchStatus.currently_running && (
              <p>Currently {formatStatus(batchStatus.currently_running.status)}: <strong>{batchStatus.currently_running.filename}</strong></p>
            )}
          </div>
        )}
        {displayedStatus && (
          <div className="modscan-progress" aria-label="Scan progress">
            <div><span>{formatStatus(displayedStatus.status)}</span><span>{displayedStatus.progress_current}/{displayedStatus.progress_total || "–"} windows</span></div>
            <progress value={displayedStatus.progress_current} max={Math.max(1, displayedStatus.progress_total)} />
            {displayedStatus.error && <p className="error-text">{displayedStatus.error}</p>}
          </div>
        )}
        {actionError && <p className="error-text">{actionError}</p>}
        {batchNotice && <p className="modscan-batch-notice">{batchNotice}</p>}
      </article>

      <div className="modscan-workspace-grid">
        <div className="modscan-results-column">
          <article className="panel">
            <div className="panel-heading">
              <div><h2>Reusable moments</h2><p>{historyScanId ? `Viewing generation ${selectedHistory?.generation ?? ""}` : "Current successful generation"}</p></div>
              <span>{segments.length} results</span>
            </div>
            <div className="modscan-filters">
              <select aria-label="Product filter" value={product} onChange={(event) => setProduct(event.target.value)}>
                <option value="">All products</option>{products.map((item) => <option key={item}>{item}</option>)}
              </select>
              <select aria-label="Role filter" value={role} onChange={(event) => setRole(event.target.value)}>
                <option value="">All roles</option>{roles.map((item) => <option key={item}>{item}</option>)}
              </select>
              <input aria-label="Minimum confidence" type="number" min="0" max="1" step="0.05" value={minimumConfidence} onChange={(event) => setMinimumConfidence(event.target.value)} />
              <input aria-label="Search moments" placeholder="Search transcript or reason" value={search} onChange={(event) => setSearch(event.target.value)} />
              <select aria-label="Sort results" value={sort} onChange={(event) => setSort(event.target.value)}>
                <option value="timestamp">Timestamp</option><option value="duration">Duration</option><option value="confidence">Confidence</option>
              </select>
            </div>
            {segmentsQuery.error && <p className="error-text">{segmentsQuery.error}</p>}
            {!sourceId ? (
              <div className="modscan-empty"><Library size={28} /><strong>Select one VOD</strong><span>Selection never starts a scan.</span></div>
            ) : segments.length === 0 ? (
              <div className="modscan-empty"><Search size={28} /><strong>No reusable moments yet</strong><span>Run Scan VOD or adjust the filters.</span></div>
            ) : (
              <div className="modscan-segment-list">
                {segments.map((segment) => (
                  <button key={segment.segment_id} className={`modscan-segment ${preview?.segment_id === segment.segment_id ? "selected" : ""}`} onClick={() => setPreview(segment)}>
                    <div className="modscan-segment-head">
                      <span>{segment.product}</span><span>{segment.role}</span><strong>{Math.round(segment.confidence * 100)}%</strong>
                    </div>
                    <div className="modscan-timing">{formatTime(segment.start_seconds)} → {formatTime(segment.end_seconds)} · {segment.duration_seconds.toFixed(3)}s</div>
                    <p>{segment.transcript_text}</p>
                    <small>{segment.reason}</small>
                    <span className="modscan-preview-label"><Play size={13} /> Preview range</span>
                  </button>
                ))}
              </div>
            )}
          </article>
        </div>

        <aside className="modscan-side-column">
          <article className="panel modscan-preview-panel">
            <div className="panel-heading"><div><h2>Source preview</h2><p>Original VOD only</p></div><Video size={18} /></div>
            {preview && sourceId ? (
              <>
                <video
                  ref={videoRef}
                  controls
                  preload="metadata"
                  src={`/api/modular-scanner/media/${sourceId}`}
                  onTimeUpdate={stopAtEnd}
                />
                <div className="modscan-preview-controls">
                  <span>{formatTime(preview.start_seconds)} → {formatTime(preview.end_seconds)}</span>
                  <button className="secondary-button" onClick={replay}><RotateCcw size={14} /> Replay</button>
                </div>
              </>
            ) : <div className="modscan-empty compact"><Play size={24} /><span>Select a result to preview its exact range.</span></div>}
          </article>

          <article className="panel">
            <div className="panel-heading"><div><h2>Scan history</h2><p>Immutable generations</p></div></div>
            <button className={`modscan-history-row ${!historyScanId ? "selected" : ""}`} onClick={() => setHistoryScanId("")} disabled={!selectedSource?.current_scan}>
              <span>Current</span><small>{selectedSource?.current_scan ? `Generation ${selectedSource.current_scan.generation}` : "None"}</small>
            </button>
            {historyRows.map((scan) => (
              <button key={scan.scan_id} className={`modscan-history-row ${historyScanId === scan.scan_id ? "selected" : ""}`} onClick={() => setHistoryScanId(scan.scan_id)}>
                <span>Generation {scan.generation}{scan.is_current ? " · current" : ""}</span>
                <small>{formatStatus(scan.status)} · {scan.accepted_count} moments</small>
              </button>
            ))}
          </article>
        </aside>
      </div>
    </section>
  );
}
