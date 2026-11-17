import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  BadgeCheck,
  ClipboardCheck,
  FileText,
  Landmark,
  MapPin,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  UserRound,
} from "lucide-react";

import { CopilotPanel } from "../components/CopilotPanel";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { createSession, errorMessage, getSession, publishScreenContext } from "../lib/api";
import { buildScreenContext } from "../lib/fields";
import type { ScreenContext, ScreenId } from "../lib/types";
import { SCREEN_TITLES, SCREEN_ORDER } from "../lib/types";
import { cn, timeLabel } from "../lib/utils";
import { useJourney, type JourneyValues } from "../store/journey";
import { AddressScreen } from "../screens/AddressScreen";
import { DocumentScreen } from "../screens/DocumentScreen";
import { MandateScreen } from "../screens/MandateScreen";
import { PanScreen } from "../screens/PanScreen";
import { ReviewScreen } from "../screens/ReviewScreen";

const SESSION_KEY = "kyc.session_id";

const STEP_META: Record<string, { label: string; icon: typeof BadgeCheck; statusKey?: string }> = {
  "kyc.pan_entry": { label: "PAN", icon: UserRound, statusKey: "pan" },
  "kyc.document_upload": { label: "Document", icon: FileText, statusKey: "document" },
  "kyc.bank_mandate": { label: "Mandate", icon: Landmark, statusKey: "mandate" },
  "kyc.address": { label: "Address", icon: MapPin, statusKey: "address" },
  "kyc.review": { label: "Review", icon: ClipboardCheck, statusKey: "journey" },
};

function statusVariant(status?: string): "success" | "warning" | "destructive" | "outline" | "secondary" {
  if (!status) return "outline";
  if (["verified", "accepted", "completed"].includes(status)) return "success";
  if (["failed", "rejected", "unknown"].includes(status)) return "destructive";
  if (["pending", "needs_review"].includes(status)) return "warning";
  return "secondary";
}

function hydrateFromContext(context: ScreenContext | null | undefined) {
  if (!context) return;
  const store = useJourney.getState();
  const values: Partial<JourneyValues> = {};
  for (const field of context.fields ?? []) {
    if (field.name in store.values) {
      values[field.name as keyof JourneyValues] = field.value ?? "";
    }
  }
  store.setValues(values);
  for (const [key, value] of Object.entries(context.status ?? {})) {
    store.setStatus(key, String(value));
  }
  if (context.document) store.setDocument(context.document);
  if (context.mandate) store.setMandate(context.mandate);
}

function StepRail() {
  const { screen, status, goToStep, mandate } = useJourney();
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Sparkles className="h-4 w-4 text-primary" /> KYC journey
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        {SCREEN_ORDER.map((id: ScreenId, index) => {
          const meta = STEP_META[id];
          const Icon = meta.icon;
          const state = meta.statusKey === "mandate" ? mandate?.status ?? status.mandate : status[meta.statusKey ?? ""];
          const active = screen === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => goToStep(index + 1)}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-xs transition",
                active ? "bg-primary/10 text-primary" : "hover:bg-muted",
              )}
            >
              <span
                className={cn(
                  "flex h-7 w-7 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold",
                  active ? "border-primary bg-primary text-primary-foreground" : "border-border bg-card",
                )}
              >
                {index + 1}
              </span>
              <Icon className="h-3.5 w-3.5 shrink-0" />
              <span className="flex-1 font-medium">{meta.label}</span>
              <Badge variant={statusVariant(state)} className="capitalize">
                {state ?? "todo"}
              </Badge>
            </button>
          );
        })}
      </CardContent>
    </Card>
  );
}

function ActivityFeed() {
  const activity = useJourney((state) => state.activity);
  const tones: Record<string, string> = {
    user: "border-primary/40 bg-primary/5",
    agent: "border-sky-200 bg-sky-50",
    action: "border-indigo-200 bg-indigo-50",
    system: "border-emerald-200 bg-emerald-50",
    error: "border-rose-200 bg-rose-50",
  };
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Journey activity</CardTitle>
      </CardHeader>
      <CardContent className="max-h-52 space-y-1.5 overflow-y-auto">
        {activity.length === 0 ? (
          <p className="text-xs text-muted-foreground">No activity yet. The copilot reads the live screen.</p>
        ) : null}
        {activity
          .slice()
          .reverse()
          .slice(0, 12)
          .map((item) => (
            <div key={item.id} className={cn("rounded-md border px-2 py-1.5 text-[11px]", tones[item.tone])}>
              <div className="flex items-center justify-between gap-2 text-[10px] opacity-60">
                <span className="uppercase tracking-wide">{item.tone}</span>
                <span>{timeLabel(item.ts)}</span>
              </div>
              <p className="mt-0.5 break-words">{item.text}</p>
            </div>
          ))}
      </CardContent>
    </Card>
  );
}

export function KycPage() {
  const navigate = useNavigate();
  const {
    sessionId,
    setSession,
    screen,
    values,
    status,
    fieldErrors,
    errors,
    document,
    mandate,
    stateOptions,
    cityOptions,
    setScreen,
    highlighted,
    reset,
    addActivity,
  } = useJourney();
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);
  const [booted, setBooted] = useState(false);
  const [contextReady, setContextReady] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const previousScreen = useRef<ScreenId | null>(null);

  useEffect(() => {
    // React StrictMode mounts effects twice in development. A ref guard here
    // would let the first (cancelled) boot win while the remount skips entirely,
    // leaving `booted=false` and the start-up spinner stuck forever. Rely on the
    // `cancelled` cleanup flag instead so each mount gets a working boot.
    let cancelled = false;

    async function boot() {
      setBooting(true);
      setBootError(null);
      const stored = sessionStorage.getItem(SESSION_KEY);
      if (stored) {
        try {
          const session = await getSession(stored);
          if (cancelled) return;
          if (session.status === "active") {
            hydrateFromContext(session.screen_context);
            setScreen(session.current_screen_id);
            setSession(session.session_id);
            setBooted(true);
            setBooting(false);
            return;
          }
        } catch {
          sessionStorage.removeItem(SESSION_KEY);
        }
      }
      try {
        const session = await createSession();
        if (cancelled) return;
        sessionStorage.setItem(SESSION_KEY, session.session_id);
        setSession(session.session_id);
        setBooted(true);
        addActivity("system", "Session created. Live screen context will publish as you type.");
      } catch (error) {
        if (!cancelled) setBootError(errorMessage(error));
      } finally {
        if (!cancelled) setBooting(false);
      }
    }

    void boot();
    return () => {
      cancelled = true;
    };
  }, [addActivity, setScreen, setSession]);

  useEffect(() => {
    if (!booted || !sessionId) return;
    const reason = previousScreen.current && previousScreen.current !== screen ? "screen_change" : "field_change";
    previousScreen.current = screen;
    const timer = window.setTimeout(() => {
      const context = buildScreenContext({
        sessionId,
        screen,
        values,
        fieldErrors,
        errors,
        status,
        document,
        mandate,
        stateOptions,
        cityOptions,
      });
      publishScreenContext(sessionId, context, reason)
        .then(() => {
          setContextReady(true);
          setPublishError(null);
        })
        .catch((error) => {
          setContextReady(false);
          setPublishError(errorMessage(error));
        });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [
    booted,
    sessionId,
    screen,
    values,
    fieldErrors,
    errors,
    status,
    document,
    mandate,
    stateOptions,
    cityOptions,
  ]);

  useEffect(() => {
    if (!highlighted) return;
    const element = window.document.querySelector(`[data-agent-field="${highlighted.field}"]`);
    element?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlighted]);

  async function newSession() {
    sessionStorage.removeItem(SESSION_KEY);
    reset();
    setBooted(false);
    setContextReady(false);
    setBooting(true);
    try {
      const session = await createSession();
      sessionStorage.setItem(SESSION_KEY, session.session_id);
      setSession(session.session_id);
      setBooted(true);
      addActivity("system", "New KYC session started.");
    } catch (error) {
      setBootError(errorMessage(error));
    } finally {
      setBooting(false);
    }
  }

  const screenComponent = {
    "kyc.pan_entry": <PanScreen key="pan" />,
    "kyc.document_upload": <DocumentScreen key="document" />,
    "kyc.bank_mandate": <MandateScreen key="mandate" />,
    "kyc.address": <AddressScreen key="address" />,
    "kyc.review": <ReviewScreen key="review" />,
  }[screen];

  return (
    <div className="mx-auto max-w-[1500px] px-4 pb-10 pt-4">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Badge variant={contextReady ? "success" : "warning"}>
            <ShieldCheck className="h-3 w-3" />
            {contextReady ? "live context published" : booting ? "starting session" : "publishing context"}
          </Badge>
          <Badge variant="outline">{SCREEN_TITLES[screen]}</Badge>
          {sessionId ? <span className="font-mono text-[10px]">session {sessionId.slice(0, 8)}</span> : null}
        </div>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => void newSession()}>
            <Plus className="h-3.5 w-3.5" /> New session
          </Button>
          <Button variant="outline" size="sm" onClick={() => navigate("/eval")}>
            Evaluation dashboard
          </Button>
        </div>
      </div>

      {bootError ? (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
          <RefreshCw className="h-3.5 w-3.5" /> Could not create a session: {bootError}
        </div>
      ) : null}
      {publishError ? (
        <div className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          The backend has not received the latest screen context yet: {publishError}
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-[250px_minmax(0,1fr)_380px]">
        <div className="space-y-4">
          <StepRail />
          <ActivityFeed />
          <Card className="hidden xl:block">
            <CardContent className="space-y-2 p-4 text-[11px] text-muted-foreground">
              <p className="flex items-center gap-1.5 font-medium text-foreground">
                <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" /> Privacy guarantees
              </p>
              <p>PAN, account, phone, e-mail and address values are replaced with format-preserving surrogates before any model call.</p>
              <p>Fill values are served over an authenticated endpoint, never through logs or SSE payloads.</p>
              <p>Financial retries always need a separate confirmation.</p>
            </CardContent>
          </Card>
        </div>

        <div className="min-w-0">
          {booting && !booted ? (
            <Card>
              <CardContent className="flex items-center gap-2 p-8 text-sm text-muted-foreground">
                <RefreshCw className="h-4 w-4 animate-spin" /> Starting an isolated KYC session…
              </CardContent>
            </Card>
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between px-1 text-[11px] text-muted-foreground">
                <span>
                  Step {Math.max(1, SCREEN_ORDER.indexOf(screen) + 1)} of {SCREEN_ORDER.length} · values auto-publish to
                  the backend
                </span>
                <Link to="/eval" className="hover:underline">
                  compare prompt v1 vs v2 →
                </Link>
              </div>
              {screenComponent}
              {!contextReady ? (
                <p className="px-1 text-[11px] text-amber-700">
                  The copilot will enable once the live context is acknowledged by the backend.
                </p>
              ) : null}
            </div>
          )}
        </div>

        <div className="min-h-[560px]">
          <CopilotPanel contextReady={contextReady && !!sessionId} />
        </div>
      </div>
    </div>
  );
}
