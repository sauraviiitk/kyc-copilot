import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  Activity,
  ArrowLeft,
  BarChart3,
  CheckCircle2,
  CircleDollarSign,
  Clock,
  FlaskConical,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  TriangleAlert,
  XCircle,
} from "lucide-react";

import { api, errorMessage } from "../lib/api";
import { cn, timeLabel } from "../lib/utils";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";

type Dict = Record<string, any>;

interface Summary {
  total: number;
  passed: number;
  failed: number;
  pass_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  avg_latency_ms: number;
  total_cost_usd: number;
  cost_per_scenario_usd: number;
  hallucinated_actions: number;
  hallucination_rate: number;
  tool_success_rate: number;
  policy_grounding_accuracy: number;
  actions_total: number;
  actions_applied: number;
  by_category?: Record<string, Dict>;
}

interface ResultRow {
  scenario_id: string;
  prompt_version: string;
  category: string;
  passed: boolean;
  latency_ms: number;
  failure_reasons?: string[];
  actual_actions?: string[];
  actual?: Dict;
  checks?: Dict;
}

interface Report {
  run: Dict;
  summaries: Record<string, Summary>;
  comparison: Dict;
  results: ResultRow[];
  generated_at?: string;
  source?: string;
}

const CATEGORIES = ["PAN", "OCR", "MANDATE", "INTENT"] as const;

function pct(value: number | undefined, digits = 1): string {
  if (value === undefined || value === null) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function num(value: number | undefined): string {
  if (value === undefined || value === null) return "—";
  return new Intl.NumberFormat().format(Math.round(value));
}

function usd(value: number | undefined): string {
  if (value === undefined || value === null) return "—";
  return `$${value.toFixed(6)}`;
}

function normalise(payload: Dict): Report {
  const summaries: Record<string, Summary> =
    payload.summary?.by_version ?? payload.summaries ?? {};
  const comparison: Dict = payload.summary?.comparison ?? payload.comparison ?? {};
  return {
    run: payload.run ?? {},
    summaries,
    comparison,
    results: (payload.results ?? []) as ResultRow[],
    generated_at: payload.generated_at,
    source: payload.source,
  };
}

function Delta({ value, lowerIsBetter = false }: { value: number | undefined; lowerIsBetter?: boolean }) {
  if (value === undefined) return null;
  const good = lowerIsBetter ? value < 0 : value > 0;
  const neutral = Math.abs(value) < 1e-9;
  return (
    <span
      className={cn(
        "text-[10px] font-semibold",
        neutral ? "text-muted-foreground" : good ? "text-emerald-600" : "text-rose-600",
      )}
    >
      {value > 0 ? "+" : ""}
      {value}
    </span>
  );
}

function MetricCard({
  label,
  icon: Icon,
  versions,
  summaries,
  accessor,
  format,
  delta,
  lowerIsBetter,
}: {
  label: string;
  icon: typeof Clock;
  versions: string[];
  summaries: Record<string, Summary>;
  accessor: (summary: Summary) => number | undefined;
  format: (value: number | undefined) => string;
  delta?: number;
  lowerIsBetter?: boolean;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
          <Icon className="h-3 w-3" /> {label}
        </div>
        <div className="mt-2 space-y-1">
          {versions.map((version, index) => (
            <div key={version} className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-muted-foreground">{version}</span>
              <span className="flex items-center gap-1.5 text-sm font-semibold">
                {format(accessor(summaries[version] ?? ({ total: 0 } as Summary)))}
                {index === versions.length - 1 && delta !== undefined ? (
                  <Delta value={delta} lowerIsBetter={lowerIsBetter} />
                ) : null}
              </span>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function ScenarioTable({ report }: { report: Report }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const versions: string[] = report.run.prompt_versions ?? ["v1", "v2"];
  const grouped = useMemo(() => {
    const map = new Map<string, { category: string; rows: Record<string, ResultRow> }>();
    for (const result of report.results) {
      const entry = map.get(result.scenario_id) ?? { category: result.category, rows: {} };
      entry.rows[result.prompt_version] = result;
      map.set(result.scenario_id, entry);
    }
    return [...map.entries()].sort((a, b) => a[1].category.localeCompare(b[1].category) || a[0].localeCompare(b[0]));
  }, [report.results]);

  if (!grouped.length) {
    return (
      <div className="rounded-lg border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
        No results yet. Run the evaluation from the panel above, or run{" "}
        <code className="rounded bg-muted px-1 py-0.5 text-[11px]">
          python -m app.evaluation.cli run --versions v1,v2
        </code>{" "}
        in <code className="rounded bg-muted px-1 py-0.5 text-[11px]">backend/</code>.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px] border-collapse text-xs">
        <thead>
          <tr className="border-b border-border text-left text-[10px] uppercase tracking-wide text-muted-foreground">
            <th className="py-2 pr-2">Scenario</th>
            <th className="px-2 py-2">Category</th>
            {versions.map((version) => (
              <th key={version} className="px-2 py-2 text-center">
                {version}
              </th>
            ))}
            <th className="py-2 pl-2 text-right">Δ pass</th>
          </tr>
        </thead>
        <tbody>
          {grouped.map(([scenarioId, entry]) => {
            const base = entry.rows[versions[0]];
            const candidate = entry.rows[versions[versions.length - 1]];
            const delta = Number(Boolean(candidate?.passed)) - Number(Boolean(base?.passed));
            const isOpen = expanded === scenarioId;
            return (
              <Fragment key={scenarioId}>
                <tr
                  className="cursor-pointer border-b border-border/60 align-top hover:bg-muted/40"
                  onClick={() => setExpanded(isOpen ? null : scenarioId)}
                >
                  <td className="py-2 pr-2 font-mono text-[11px]">{scenarioId}</td>
                  <td className="px-2 py-2">
                    <Badge variant="outline">{entry.category}</Badge>
                  </td>
                  {versions.map((version) => {
                    const row = entry.rows[version];
                    return (
                      <td key={version} className="px-2 py-2 text-center">
                        {row ? (
                          <div className="flex flex-col items-center gap-0.5">
                            <Badge variant={row.passed ? "success" : "destructive"}>
                              {row.passed ? <CheckCircle2 className="h-3 w-3" /> : <XCircle className="h-3 w-3" />}
                              {row.passed ? "pass" : "fail"}
                            </Badge>
                            <span className="text-[10px] text-muted-foreground">{row.latency_ms} ms</span>
                          </div>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                    );
                  })}
                  <td className="py-2 pl-2 text-right">
                    {delta > 0 ? (
                      <span className="font-semibold text-emerald-600">+1</span>
                    ) : delta < 0 ? (
                      <span className="font-semibold text-rose-600">−1</span>
                    ) : (
                      <span className="text-muted-foreground">0</span>
                    )}
                  </td>
                </tr>
                {isOpen ? (
                  <tr className="border-b border-border/60 bg-muted/20">
                    <td colSpan={3 + versions.length} className="p-3">
                      <div className="grid gap-3 md:grid-cols-2">
                        {versions.map((version) => {
                          const row = entry.rows[version];
                          if (!row) return null;
                          return (
                            <div key={version} className="space-y-1 rounded-lg bg-card p-2.5">
                              <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                                {version} · actions:{" "}
                                <span className="font-mono normal-case">
                                  {row.actual_actions?.join(", ") || "none"}
                                </span>
                              </p>
                              {row.failure_reasons?.length ? (
                                <ul className="list-disc space-y-0.5 pl-4 text-[11px] text-rose-700">
                                  {row.failure_reasons.map((reason) => (
                                    <li key={reason}>{reason}</li>
                                  ))}
                                </ul>
                              ) : (
                                <p className="text-[11px] text-emerald-700">All expectation checks passed.</p>
                              )}
                              <p className="text-[10px] text-muted-foreground">
                                policy refs:{" "}
                                <span className="font-mono">
                                  {(row.actual?.policy_refs ?? []).join(", ") || "none"}
                                </span>
                              </p>
                              <p className="text-[10px] text-muted-foreground">
                                message: {row.actual?.say_masked ?? "—"}
                              </p>
                            </div>
                          );
                        })}
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function EvalDashboard() {
  const [report, setReport] = useState<Report | null>(null);
  const [scenarios, setScenarios] = useState<Dict[]>([]);
  const [runs, setRuns] = useState<Dict[]>([]);
  const [loading, setLoading] = useState(true);
  const [polling, setPolling] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([...CATEGORIES]);
  const [versions, setVersions] = useState<string[]>(["v1", "v2"]);
  const [label, setLabel] = useState("");
  const previousRunId = useRef<string | null>(null);
  const pollDeadline = useRef<number | null>(null);

  const loadLatest = useCallback(async () => {
    const { data } = await api.get("/eval/latest");
    const next = normalise(data);
    setReport(next);
    return next;
  }, []);

  const loadSidebar = useCallback(async () => {
    const [scenarioResponse, runsResponse] = await Promise.allSettled([
      api.get("/eval/scenarios"),
      api.get("/eval/runs", { params: { limit: 8 } }),
    ]);
    if (scenarioResponse.status === "fulfilled") {
      setScenarios(scenarioResponse.value.data.scenarios ?? []);
    }
    if (runsResponse.status === "fulfilled") {
      setRuns(runsResponse.value.data.runs ?? []);
    }
  }, []);

  useEffect(() => {
    Promise.allSettled([loadLatest(), loadSidebar()])
      .catch(() => undefined)
      .finally(() => setLoading(false));
  }, [loadLatest, loadSidebar]);

  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await loadLatest();
        void loadSidebar();
        // Compare run ids rather than timestamps: browser/server clocks can
        // differ, which would otherwise make `isNew` false forever.
        const currentId = next.run?.id as string | undefined;
        const status = next.run?.status as string | undefined;
        const isNew = previousRunId.current
          ? Boolean(currentId && currentId !== previousRunId.current)
          : true;
        const terminal = status === "completed" || status === "failed";
        const timedOut = pollDeadline.current !== null && Date.now() > pollDeadline.current;
        if ((isNew && terminal) || timedOut) {
          setPolling(false);
          setRunning(false);
          if (status !== "completed") {
            setError(
              timedOut
                ? "Evaluation timed out. Check the backend logs."
                : "The evaluation run failed or was interrupted. See Recent runs for details.",
            );
          }
        }
      } catch (err) {
        setError(errorMessage(err));
      }
    }, 2500);
    return () => window.clearInterval(timer);
  }, [polling, loadLatest, loadSidebar]);

  async function startRun() {
    if (!selectedCategories.length || !versions.length) return;
    setRunning(true);
    setError(null);
    previousRunId.current = (report?.run?.id as string | undefined) ?? null;
    pollDeadline.current = Date.now() + 15 * 60 * 1000;
    try {
      await api.post("/eval/runs", {
        label: label.trim() || null,
        prompt_versions: versions,
        categories: selectedCategories,
        scenario_ids: null,
      });
      setPolling(true);
    } catch (err) {
      setRunning(false);
      setError(errorMessage(err));
    }
  }

  const versionKeys = report ? Object.keys(report.summaries).sort() : [];
  const summaries = report?.summaries ?? {};
  const summaryDeltas: Record<string, number> =
    report?.comparison?.deltas?.[`${versionKeys[0]}->${versionKeys[versionKeys.length - 1]}`] ?? {};

  const latest = report?.run ?? {};
  const perScenario = report?.comparison?.per_scenario ?? {};
  const improved: string[] = perScenario.improved ?? [];
  const regressed: string[] = perScenario.regressed ?? [];
  const categoryRows = useMemo(() => {
    if (!report) return [];
    const cats = new Set<string>();
    for (const summary of Object.values(report.summaries)) {
      Object.keys(summary.by_category ?? {}).forEach((category) => cats.add(category));
    }
    return [...cats].sort();
  }, [report]);

  return (
    <div className="mx-auto max-w-[1500px] px-4 pb-12 pt-4">
      <div className="mb-5 flex flex-wrap items-start justify-between gap-3">
        <div>
          <Link to="/" className="mb-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:underline">
            <ArrowLeft className="h-3 w-3" /> Back to the KYC journey
          </Link>
          <h1 className="flex items-center gap-2 text-xl font-semibold">
            <FlaskConical className="h-5 w-5 text-primary" /> Evaluation dashboard
          </h1>
          <p className="mt-1 max-w-2xl text-xs text-muted-foreground">
            Real executions of the offline planner (or your configured model) against {scenarios.length || 32} scenarios.
            v1 and v2 run in isolated sessions through the same action protocol as the browser; no metric is hardcoded.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {latest.status ? (
            <Badge variant={latest.status === "completed" ? "success" : latest.status === "running" ? "warning" : "outline"}>
              latest: {latest.status}
            </Badge>
          ) : null}
          <Button variant="outline" size="sm" onClick={() => void Promise.allSettled([loadLatest(), loadSidebar()])}>
            <RefreshCw className="h-3.5 w-3.5" /> Refresh
          </Button>
        </div>
      </div>

      {error ? (
        <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">{error}</div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <div className="flex items-center justify-between">
                <div>
                  <CardTitle className="text-sm">Headline metrics</CardTitle>
                  <CardDescription>
                    {latest.label ? `${latest.label} · ` : ""}
                    {latest.provider ?? "—"} / {latest.model ?? "—"} · run{" "}
                    <span className="font-mono">{String(latest.id ?? "").slice(0, 8)}</span>
                  </CardDescription>
                </div>
                {latest.started_at ? (
                  <span className="flex items-center gap-1 text-[10px] text-muted-foreground">
                    <Clock className="h-3 w-3" /> {timeLabel(latest.started_at)}
                  </span>
                ) : null}
              </div>
            </CardHeader>
            <CardContent>
              {loading ? (
                <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> Loading the latest report…
                </div>
              ) : !versionKeys.length ? (
                <div className="py-8 text-center text-sm text-muted-foreground">
                  No report yet. Start a run from the panel on the right.
                </div>
              ) : (
                <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                  <MetricCard
                    label="Pass rate"
                    icon={Activity}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.pass_rate}
                    format={(value) => pct(value)}
                    delta={summaryDeltas.pass_rate}
                  />
                  <MetricCard
                    label="p95 latency"
                    icon={Clock}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.p95_latency_ms}
                    format={(value) => `${num(value)} ms`}
                    delta={summaryDeltas.p95_latency_ms}
                    lowerIsBetter
                  />
                  <MetricCard
                    label="Hallucinated actions"
                    icon={TriangleAlert}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.hallucination_rate}
                    format={(value) => pct(value)}
                    delta={summaryDeltas.hallucination_rate}
                    lowerIsBetter
                  />
                  <MetricCard
                    label="Policy grounding"
                    icon={ShieldCheck}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.policy_grounding_accuracy}
                    format={(value) => pct(value)}
                    delta={summaryDeltas.policy_grounding_accuracy}
                  />
                  <MetricCard
                    label="Tool success"
                    icon={CheckCircle2}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.tool_success_rate}
                    format={(value) => pct(value)}
                    delta={summaryDeltas.tool_success_rate}
                  />
                  <MetricCard
                    label="p50 latency"
                    icon={Clock}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.p50_latency_ms}
                    format={(value) => `${num(value)} ms`}
                    delta={summaryDeltas.p50_latency_ms}
                    lowerIsBetter
                  />
                  <MetricCard
                    label="Total cost"
                    icon={CircleDollarSign}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.total_cost_usd}
                    format={(value) => usd(value)}
                    delta={summaryDeltas.total_cost_usd}
                    lowerIsBetter
                  />
                  <MetricCard
                    label="Passed / total"
                    icon={BarChart3}
                    versions={versionKeys}
                    summaries={summaries}
                    accessor={(summary) => summary.passed}
                    format={(value) => `${num(value)} / ${num(summaries[versionKeys[versionKeys.length - 1]]?.total ?? 0)}`}
                  />
                </div>
              )}

              {versionKeys.length > 1 ? (
                <div className="mt-4 flex flex-wrap items-center gap-2 rounded-lg bg-muted/50 px-3 py-2 text-[11px]">
                  <span className="font-semibold">
                    {versionKeys[0]} → {versionKeys[versionKeys.length - 1]}:
                  </span>
                  <span className="text-muted-foreground">pass-rate delta</span>
                  <Delta value={summaryDeltas.pass_rate} />
                  <span className="text-muted-foreground">improved scenarios</span>
                  <Badge variant="success">{improved.length}</Badge>
                  <span className="text-muted-foreground">regressed</span>
                  <Badge variant={regressed.length ? "destructive" : "secondary"}>{regressed.length}</Badge>
                  {improved.slice(0, 6).map((id) => (
                    <span key={id} className="rounded bg-card px-1.5 py-0.5 font-mono text-[10px]">
                      {id}
                    </span>
                  ))}
                </div>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Per-scenario comparison</CardTitle>
              <CardDescription>Click a row to inspect executed actions and failure reasons.</CardDescription>
            </CardHeader>
            <CardContent>
              {report ? <ScenarioTable report={report} /> : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Category breakdown</CardTitle>
            </CardHeader>
            <CardContent>
              {categoryRows.length ? (
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-left text-[10px] uppercase tracking-wide text-muted-foreground">
                      <th className="py-1.5">Category</th>
                      {versionKeys.map((version) => (
                        <th key={version} className="py-1.5 text-right">
                          {version}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {categoryRows.map((category) => (
                      <tr key={category} className="border-t border-border/60">
                        <td className="py-1.5 font-medium">{category}</td>
                        {versionKeys.map((version) => {
                          const block = report?.summaries[version]?.by_category?.[category];
                          return (
                            <td key={version} className="py-1.5 text-right">
                              {block
                                ? `${block.passed}/${block.total} (${pct(block.pass_rate, 0)}) · ${block.p95_latency_ms}ms`
                                : "—"}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-xs text-muted-foreground">No category data yet.</p>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Start an evaluation</CardTitle>
              <CardDescription>
                Runs in a background worker; this page polls until the report is written.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div>
                <p className="label-text">Label</p>
                <Input
                  value={label}
                  onChange={(event) => setLabel(event.target.value)}
                  placeholder="e.g. policy prompt v2 rollout"
                  className="h-9 text-xs"
                />
              </div>
              <div>
                <p className="label-text">Prompt versions</p>
                <div className="flex gap-3 text-xs">
                  {["v1", "v2"].map((version) => (
                    <label key={version} className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={versions.includes(version)}
                        onChange={(event) =>
                          setVersions((prev) =>
                            event.target.checked ? [...new Set([...prev, version])] : prev.filter((v) => v !== version),
                          )
                        }
                      />
                      {version}
                    </label>
                  ))}
                </div>
              </div>
              <div>
                <p className="label-text">Categories</p>
                <div className="grid grid-cols-2 gap-2 text-xs">
                  {CATEGORIES.map((category) => (
                    <label key={category} className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={selectedCategories.includes(category)}
                        onChange={(event) =>
                          setSelectedCategories((prev) =>
                            event.target.checked ? [...new Set([...prev, category])] : prev.filter((c) => c !== category),
                          )
                        }
                      />
                      {category}
                    </label>
                  ))}
                </div>
              </div>
              <p className="text-[11px] text-muted-foreground">
                {scenarios.length} scenarios loaded · {selectedCategories.length || 0} categories ·{" "}
                {versions.length || 0} versions
              </p>
              <Button
                className="w-full"
                size="sm"
                disabled={running || !selectedCategories.length || !versions.length || polling}
                onClick={() => void startRun()}
              >
                {running || polling ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                {polling ? "Running evaluation…" : "Run evaluation"}
              </Button>
              {polling ? (
                <p className="text-[10px] text-muted-foreground">
                  Executing scenarios in isolated sessions. This can take a minute; results appear automatically.
                </p>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-sm">Recent runs</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {runs.length === 0 ? (
                <p className="text-xs text-muted-foreground">No evaluation runs recorded yet.</p>
              ) : (
                runs.map((run) => (
                  <div key={run.id} className="rounded-lg border border-border px-2.5 py-2 text-[11px]">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{run.label ?? run.id.slice(0, 8)}</span>
                      <Badge variant={run.status === "completed" ? "success" : "warning"}>{run.status}</Badge>
                    </div>
                    <p className="mt-0.5 text-muted-foreground">
                      {run.passed}/{run.total} passed · {run.prompt_versions?.join(", ")} · p95 {run.p95_latency_ms} ms
                    </p>
                    <p className="text-[10px] text-muted-foreground">
                      {run.provider}/{run.model} · {timeLabel(run.started_at)}
                    </p>
                  </div>
                ))
              )}
            </CardContent>
          </Card>

          {scenarioCountLabel(scenarios) ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Method</CardTitle>
              </CardHeader>
              <CardContent className="space-y-1.5 text-[11px] text-muted-foreground">
                <p>
                  <span className="font-semibold text-foreground">{scenarioCountLabel(scenarios)}</span> scenarios across
                  PAN, OCR, MANDATE and INTENT.
                </p>
                <p>Every scenario seeds the real screen state, runs the LangGraph agent and executes actions via the client protocol.</p>
                <p>Financial retries are confirmed by the headless user and verified server-side.</p>
                <p>Only masked previews are stored in reports.</p>
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function scenarioCountLabel(scenarios: Dict[]): string {
  return scenarios.length ? String(scenarios.length) : "";
}
