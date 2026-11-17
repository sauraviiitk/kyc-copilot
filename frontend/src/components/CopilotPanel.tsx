import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  CheckCircle2,
  ChevronDown,
  Loader2,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Upload,
  UserRound,
  X,
} from "lucide-react";

import {
  errorMessage,
  fetchActionValue,
  fetchHealth,
  reportActionResult,
  retryMandate,
  startAgentRun,
} from "../lib/api";
import type { AgentAction, Activity, HealthResponse, ScreenId } from "../lib/types";
import { newId } from "../lib/utils";
import { useJourney } from "../store/journey";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card";
import { Select, Textarea } from "./ui/input";

const EVENT_NAMES = [
  "agent_status",
  "context",
  "policy",
  "agent_plan",
  "action",
  "action_result",
  "agent_message",
  "agent_complete",
  "error",
  "stream_end",
] as const;

interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  text: string;
}

const SCREEN_IDS: ScreenId[] = [
  "kyc.pan_entry",
  "kyc.document_upload",
  "kyc.bank_mandate",
  "kyc.address",
  "kyc.review",
];

function actionLabel(action: AgentAction): string {
  const subject = action.field ?? action.target ?? "";
  switch (action.type) {
    case "fill":
      return `Fill ${subject}`;
    case "highlight":
      return `Highlight ${subject}`;
    case "upload":
      return "Open document uploader";
    case "retry":
      return "Retry bank mandate";
    case "navigate":
      return `Open ${subject.replace("kyc.", "").replace(/_/g, " ")}`;
    case "escalate":
      return "Escalate to human support";
    case "submit":
      return "Submit step";
    default:
      return String(action.type);
  }
}

export function CopilotPanel({ contextReady }: { contextReady: boolean }) {
  const sessionId = useJourney((state) => state.sessionId);
  const screen = useJourney((state) => state.screen);
  const [messages, setMessages] = useState<Message[]>([
    {
      id: "welcome",
      role: "assistant",
      text: "Hi! I can see your live KYC screen. Tell me what is failing, or ask what a policy means.",
    },
  ]);
  const [input, setInput] = useState("");
  const [actions, setActions] = useState<AgentAction[]>([]);
  const [policyRefs, setPolicyRefs] = useState<string[]>([]);
  const [plan, setPlan] = useState<string | null>(null);
  const [confidence, setConfidence] = useState<number | null>(null);
  const [statusLine, setStatusLine] = useState<string>("idle");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmationPrompt, setConfirmationPrompt] = useState<string | null>(null);
  const [retryBusy, setRetryBusy] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  const [promptVersion, setPromptVersion] = useState<"v1" | "v2">(
    () => (localStorage.getItem("copilot.prompt_version") as "v1" | "v2") || "v2",
  );

  const runIdRef = useRef<string | null>(null);
  const sourceRef = useRef<EventSource | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    fetchHealth()
      .then(setHealth)
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, actions]);

  useEffect(() => () => sourceRef.current?.close(), []);

  const addMessage = useCallback((role: Message["role"], text: string) => {
    setMessages((prev) => [...prev, { id: newId(), role, text }]);
  }, []);

  const upsertAction = useCallback((action: AgentAction) => {
    setActions((prev) => {
      const index = prev.findIndex((item) => item.action_id === action.action_id);
      if (index === -1) return [...prev, action];
      const next = [...prev];
      next[index] = { ...next[index], ...action };
      return next;
    });
  }, []);

  const notify = useCallback((text: string, tone: Activity["tone"] = "system") => {
    useJourney.getState().addActivity(tone, text);
  }, []);

  const executeAction = useCallback(
    async (action: AgentAction, runId: string) => {
      const store = useJourney.getState();
      const activeSession = store.sessionId;
      if (!activeSession) return;
      upsertAction(action);

      try {
        if (action.type === "fill") {
          const payload = await fetchActionValue(action.action_id, activeSession);
          if (payload.field) {
            store.setValue(payload.field as never, payload.value);
            store.highlight(payload.field);
          }
          await reportActionResult({
            run_id: runId,
            action_id: action.action_id,
            session_id: activeSession,
            status: "applied",
            executed_value_hash: payload.value_hash,
            detail: { screen_id: store.screen },
          });
          upsertAction({ ...action, status: "applied" });
          notify(`Copilot filled ${payload.field ?? "a field"} after your confirmation.`);
        } else if (action.type === "highlight") {
          if (action.field) store.highlight(action.field);
          await reportActionResult({
            run_id: runId,
            action_id: action.action_id,
            session_id: activeSession,
            status: "applied",
            detail: { screen_id: store.screen },
          });
          upsertAction({ ...action, status: "applied" });
        } else if (action.type === "navigate") {
          const target = (action.target ?? "") as ScreenId;
          if (SCREEN_IDS.includes(target)) {
            store.setScreen(target, "copilot navigation");
          }
          await reportActionResult({
            run_id: runId,
            action_id: action.action_id,
            session_id: activeSession,
            status: "applied",
            detail: { screen_id: SCREEN_IDS.includes(target) ? target : store.screen },
          });
          upsertAction({ ...action, status: "applied" });
        } else if (action.type === "upload") {
          if (store.screen !== "kyc.document_upload") {
            store.setScreen("kyc.document_upload", "copilot re-upload");
          }
          store.setPendingUpload({ runId, actionId: action.action_id });
          window.setTimeout(() => useJourney.getState().requestUpload(), 450);
          upsertAction({ ...action, status: "dispatched" });
          notify("Uploader requested — choose a clearer document when the file picker opens.");
        } else if (action.type === "retry" || action.type === "submit") {
          upsertAction({ ...action, status: "pending_confirmation" });
        } else if (action.type === "escalate") {
          await reportActionResult({
            run_id: runId,
            action_id: action.action_id,
            session_id: activeSession,
            status: "applied",
            detail: { message: action.reason ?? "Escalated to human support" },
          });
          upsertAction({ ...action, status: "applied" });
          notify("Escalated to human support.");
        }
      } catch (err) {
        setError(errorMessage(err));
        upsertAction({ ...action, status: "failed" });
        await reportActionResult({
          run_id: runId,
          action_id: action.action_id,
          session_id: activeSession,
          status: "failed",
          detail: { error: errorMessage(err) },
        }).catch(() => undefined);
      }
    },
    [notify, upsertAction],
  );

  const handleEvent = useCallback(
    (name: string, data: Record<string, any>) => {
      const runId = runIdRef.current;
      if (name === "agent_status") {
        setStatusLine(String(data.status ?? "working"));
      } else if (name === "policy") {
        setPolicyRefs((data.refs as string[]) ?? []);
      } else if (name === "agent_plan") {
        setPlan(String(data.plan ?? ""));
        setConfidence(typeof data.confidence === "number" ? data.confidence : null);
      } else if (name === "agent_message") {
        addMessage("assistant", String(data.say ?? ""));
      } else if (name === "action" && runId) {
        const action = data as unknown as AgentAction;
        void executeAction(action, runId);
      } else if (name === "agent_complete") {
        setRunning(false);
        setStatusLine(String(data.status ?? "completed"));
        if (data.requires_confirmation && data.confirmation_prompt) {
          setConfirmationPrompt(String(data.confirmation_prompt));
        }
        if (data.degraded) {
          addMessage("system", "The live model was unavailable, so the deterministic planner answered.");
        }
      } else if (name === "action_result") {
        setActions((prev) =>
          prev.map((item) =>
            item.action_id === data.action_id
              ? { ...item, status: String(data.status ?? item.status) }
              : item,
          ),
        );
      } else if (name === "error") {
        setError(String(data.message ?? "The copilot hit an error."));
      } else if (name === "stream_end") {
        setRunning(false);
        sourceRef.current?.close();
      }
    },
    [addMessage, executeAction],
  );

  async function send() {
    const text = input.trim();
    if (!text || !sessionId || running) return;
    setInput("");
    setError(null);
    setPlan(null);
    setPolicyRefs([]);
    setConfirmationPrompt(null);
    setActions([]);
    setRunning(true);
    setStatusLine("starting");
    addMessage("user", text);
    notify(text, "user");
    try {
      const run = await startAgentRun({
        session_id: sessionId,
        message: text,
        prompt_version: promptVersion,
      });
      runIdRef.current = run.run_id;
      sourceRef.current?.close();
      const source = new EventSource(run.stream_url);
      sourceRef.current = source;
      for (const name of EVENT_NAMES) {
        source.addEventListener(name, (event) => {
          try {
            handleEvent(name, JSON.parse((event as MessageEvent).data));
          } catch {
            // Ignore malformed frames; the stream continues.
          }
        });
      }
      source.onerror = () => {
        setRunning(false);
        source.close();
      };
    } catch (err) {
      setRunning(false);
      setError(errorMessage(err));
      addMessage("system", errorMessage(err));
    }
  }

  async function confirmRetry(action: AgentAction) {
    const activeSession = useJourney.getState().sessionId;
    const runId = runIdRef.current;
    if (!activeSession || !runId || !action.idempotency_key) return;
    setRetryBusy(action.action_id);
    try {
      const outcome = await retryMandate(activeSession, action.idempotency_key);
      const store = useJourney.getState();
      store.setMandate({
        mandate_ref: outcome.mandate_ref,
        status: outcome.status,
        attempts: outcome.attempts,
        max_attempts: outcome.max_attempts,
        last_error: outcome.status === "verified" ? null : outcome.message,
        idempotency_key: outcome.idempotency_key,
      });
      store.setStatus("mandate", outcome.status);
      await reportActionResult({
        run_id: runId,
        action_id: action.action_id,
        session_id: activeSession,
        status: outcome.status === "unknown" ? "failed" : "applied",
        detail: { mandate_ref: outcome.mandate_ref, attempts: outcome.attempts },
      });
      upsertAction({ ...action, status: outcome.status === "unknown" ? "failed" : "applied" });
      addMessage(
        "assistant",
        outcome.status === "verified"
          ? "Mandate verified after your confirmed retry."
          : `Retry finished with status “${outcome.status}”. ${outcome.message}`,
      );
      notify(`Confirmed mandate retry → ${outcome.status}.`);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setRetryBusy(null);
    }
  }

  async function rejectAction(action: AgentAction) {
    const activeSession = useJourney.getState().sessionId;
    const runId = runIdRef.current;
    if (!activeSession || !runId) return;
    await reportActionResult({
      run_id: runId,
      action_id: action.action_id,
      session_id: activeSession,
      status: "rejected",
      detail: { reason: "user cancelled" },
    }).catch(() => undefined);
    upsertAction({ ...action, status: "rejected" });
    addMessage("assistant", "No problem — nothing was submitted.");
  }

  const provider = health?.provider ?? "mock";
  const model = health?.model ?? "deterministic-planner";
  const retryActions = actions.filter(
    (action) => action.type === "retry" || (action.type === "submit" && action.requires_confirmation),
  );

  return (
    <Card className="flex h-full min-h-[560px] flex-col overflow-hidden">
      <CardHeader className="border-b border-border/70">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="flex items-center gap-2">
            <span className="rounded-lg bg-primary/10 p-1.5 text-primary">
              <Sparkles className="h-4 w-4" />
            </span>
            KYC Copilot
          </CardTitle>
          <Badge variant={running ? "warning" : "secondary"}>{running ? statusLine : "ready"}</Badge>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 pt-1 text-[11px] text-muted-foreground">
          <Badge variant="outline">
            <Bot className="h-3 w-3" /> {provider}
          </Badge>
          <span className="truncate">{model}</span>
          <span>· policy: {health?.policy_backend ?? "local"}</span>
        </div>
      </CardHeader>

      <CardContent className="flex min-h-0 flex-1 flex-col gap-3 p-0">
        <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-3">
          {messages.map((message) => (
            <div
              key={message.id}
              className={
                message.role === "user"
                  ? "ml-auto max-w-[88%] rounded-2xl rounded-br-sm bg-primary px-3 py-2 text-xs text-primary-foreground"
                  : message.role === "assistant"
                    ? "max-w-[92%] rounded-2xl rounded-bl-sm bg-muted px-3 py-2 text-xs leading-relaxed"
                    : "rounded-lg border border-dashed border-border px-3 py-2 text-[11px] text-muted-foreground"
              }
            >
              <div className="mb-0.5 flex items-center gap-1.5 text-[10px] opacity-70">
                {message.role === "user" ? (
                  <>
                    <UserRound className="h-3 w-3" /> you
                  </>
                ) : message.role === "assistant" ? (
                  <>
                    <Bot className="h-3 w-3" /> copilot
                  </>
                ) : (
                  <>system</>
                )}
              </div>
              <p className="whitespace-pre-wrap">{message.text}</p>
            </div>
          ))}

          {actions.length ? (
            <div className="space-y-2">
              <p className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                Actions on the live screen
              </p>
              {actions.map((action) => (
                <div key={action.action_id} className="rounded-lg border border-border bg-card px-2.5 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 text-xs font-medium">
                      {action.type === "fill" ? <CheckCircle2 className="h-3.5 w-3.5 text-primary" /> : null}
                      {action.type === "upload" ? <Upload className="h-3.5 w-3.5 text-primary" /> : null}
                      {action.type === "retry" ? <RotateCcw className="h-3.5 w-3.5 text-amber-600" /> : null}
                      {action.type === "navigate" ? <ArrowRight className="h-3.5 w-3.5 text-primary" /> : null}
                      {action.type === "escalate" ? <AlertTriangle className="h-3.5 w-3.5 text-amber-600" /> : null}
                      {actionLabel(action)}
                    </span>
                    <Badge
                      variant={
                        action.status === "applied"
                          ? "success"
                          : action.status === "failed" || action.status === "rejected"
                            ? "destructive"
                            : action.status === "pending_confirmation"
                              ? "warning"
                              : "secondary"
                      }
                    >
                      {action.status ?? "proposed"}
                    </Badge>
                  </div>
                  <p className="mt-1 text-[11px] text-muted-foreground">{action.reason}</p>
                  {action.preview ? (
                    <p className="mt-1 font-mono text-[10px] text-muted-foreground">preview: {action.preview}</p>
                  ) : null}
                  {action.type === "upload" && action.status !== "applied" ? (
                    <Button
                      size="sm"
                      variant="outline"
                      className="mt-2 h-7 text-[11px]"
                      onClick={() => useJourney.getState().requestUpload()}
                    >
                      <Upload className="h-3 w-3" /> Open uploader
                    </Button>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}

          {retryActions.length ? (
            <div className="space-y-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-amber-900">
              <p className="flex items-center gap-1.5 text-xs font-semibold">
                <AlertTriangle className="h-3.5 w-3.5" /> Confirmation required
              </p>
              <p className="text-[11px]">{confirmationPrompt ?? "This financial action needs your explicit confirmation."}</p>
              {retryActions.map((action) => (
                <div key={action.action_id} className="flex gap-2">
                  <Button
                    size="sm"
                    variant="destructive"
                    className="h-7 text-[11px]"
                    disabled={retryBusy === action.action_id || action.status !== "pending_confirmation"}
                    onClick={() => void confirmRetry(action)}
                  >
                    {retryBusy === action.action_id ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
                    Confirm retry
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 text-[11px]"
                    disabled={action.status !== "pending_confirmation"}
                    onClick={() => void rejectAction(action)}
                  >
                    <X className="h-3 w-3" /> Cancel
                  </Button>
                </div>
              ))}
            </div>
          ) : null}

          {error ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-800">
              {error}
            </div>
          ) : null}
        </div>

        <div className="border-t border-border/70 px-4 py-3">
          {policyRefs.length ? (
            <div className="mb-2 flex flex-wrap gap-1">
              {policyRefs.slice(0, 4).map((ref) => (
                <Badge key={ref} variant="outline" className="font-mono text-[10px]">
                  {ref}
                </Badge>
              ))}
            </div>
          ) : null}

          <button
            type="button"
            className="mb-2 flex w-full items-center justify-between text-[10px] text-muted-foreground"
            onClick={() => setShowDetails((value) => !value)}
          >
            <span className="inline-flex items-center gap-1">
              <ShieldCheck className="h-3 w-3 text-emerald-600" /> PII masked · prompt {promptVersion}
              {confidence !== null ? ` · confidence ${confidence.toFixed(2)}` : ""}
            </span>
            <ChevronDown className={showDetails ? "h-3 w-3 rotate-180 transition" : "h-3 w-3 transition"} />
          </button>
          {showDetails ? (
            <div className="mb-2 space-y-1 rounded-lg bg-muted/50 p-2 text-[10px] text-muted-foreground">
              <p>
                <span className="font-semibold">Screen:</span> {screen}
              </p>
              <p>
                <span className="font-semibold">Plan:</span> {plan ?? "—"}
              </p>
              <p>
                <span className="font-semibold">Context:</span> {contextReady ? "live and published" : "publishing…"}
              </p>
              <p>Raw PAN, account and address values never leave the server unmasked for model calls.</p>
            </div>
          ) : null}

          <Textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
            placeholder="e.g. my PAN is being rejected, help me fix it"
            className="min-h-[64px] text-xs"
            maxLength={2000}
          />
          <div className="mt-2 flex items-center gap-2">
            <Select
              value={promptVersion}
              onChange={(event) => {
                const value = event.target.value as "v1" | "v2";
                setPromptVersion(value);
                localStorage.setItem("copilot.prompt_version", value);
              }}
              className="h-8 w-24 text-[11px]"
              title="Prompt version (evaluation toggle)"
            >
              <option value="v2">v2</option>
              <option value="v1">v1</option>
            </Select>
            <Button
              onClick={() => void send()}
              disabled={running || !input.trim() || !sessionId || !contextReady}
              className="ml-auto h-8"
              size="sm"
            >
              {running ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
              Send
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
