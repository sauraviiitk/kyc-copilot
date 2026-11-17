import { useEffect, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, FileText, Loader2, RotateCcw, Upload } from "lucide-react";

import { acceptDocument, errorMessage, reportActionResult, uploadDocument } from "../lib/api";
import type { DocumentUploadResponse } from "../lib/types";
import { useJourney } from "../store/journey";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { InlineAlert } from "../components/ui/field";

function Metric({ label, value, threshold, higher = true }: { label: string; value?: number | null; threshold: number; higher?: boolean }) {
  if (value === null || value === undefined) return null;
  const ok = higher ? value >= threshold : value <= threshold;
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-2.5">
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className={ok ? "text-sm font-semibold text-emerald-700" : "text-sm font-semibold text-rose-600"}>
        {typeof value === "number" && value <= 1 ? value.toFixed(2) : value}
      </p>
    </div>
  );
}

export function DocumentScreen() {
  const { sessionId, document, setDocument, setStatus, status, setErrors, uploadSignal, pendingUpload, addActivity, nextScreen } = useJourney();
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const [result, setResult] = useState<DocumentUploadResponse | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (uploadSignal > 0) {
      inputRef.current?.click();
    }
  }, [uploadSignal]);

  async function handleFile(file: File | null) {
    if (!file || !sessionId) return;
    setBusy(true);
    setMessage(null);
    try {
      const response = await uploadDocument(sessionId, file);
      setResult(response);
      setDocument({
        document_id: response.document_id,
        filename: response.filename,
        doc_type: response.doc_type,
        status: response.status,
        quality_score: response.quality.quality_score,
        ocr_confidence: response.quality.ocr_confidence,
        rotation_degrees: response.quality.rotation_degrees,
        blur_score: response.quality.blur_score,
        issues: response.quality.issues,
      });
      setStatus("document", response.status);
      setErrors(response.quality.issues);
      addActivity(
        response.status === "accepted" ? "system" : "error",
        `Document ${response.filename} → ${response.status}`,
      );
      const pending = useJourney.getState().pendingUpload;
      if (pending) {
        await reportActionResult({
          run_id: pending.runId,
          action_id: pending.actionId,
          session_id: sessionId,
          status: response.status === "accepted" ? "applied" : "failed",
          detail: { screen_id: "kyc.document_upload", document_id: response.document_id },
        }).catch(() => undefined);
        useJourney.setState({ pendingUpload: null });
      }
    } catch (error) {
      setMessage(errorMessage(error));
      addActivity("error", errorMessage(error));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function accept() {
    const doc = result ?? document;
    if (!doc?.document_id || !sessionId) return;
    setAccepting(true);
    try {
      await acceptDocument(sessionId, doc.document_id);
      setStatus("document", "accepted");
      addActivity("system", "Document extraction accepted. Moving to bank mandate.");
      window.setTimeout(() => nextScreen(), 500);
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setAccepting(false);
    }
  }

  const quality = result?.quality;
  const issues = quality?.issues ?? document?.issues ?? [];
  const accepted = status.document === "accepted";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Upload identity document</CardTitle>
            <CardDescription>Step 2 of 5 · OCR runs locally on a simulated provider.</CardDescription>
          </div>
          <Badge variant={accepted ? "success" : status.document === "rejected" ? "destructive" : "outline"}>
            {status.document ?? "not started"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {message ? <InlineAlert tone="error">{message}</InlineAlert> : null}

        {pendingUpload ? (
          <InlineAlert tone="warning" title="The copilot requested a re-upload">
            Nothing is uploaded automatically. Choose a clearer document to continue.
          </InlineAlert>
        ) : null}

        <input
          ref={inputRef}
          type="file"
          accept="image/jpeg,image/png,application/pdf"
          className="hidden"
          onChange={(event) => handleFile(event.target.files?.[0] ?? null)}
        />

        <div
          className="flex flex-col items-center justify-center gap-2 rounded-xl border-2 border-dashed border-border bg-muted/20 px-4 py-8 text-center"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            handleFile(event.dataTransfer.files?.[0] ?? null);
          }}
        >
          <div className="rounded-full bg-primary/10 p-3 text-primary">
            <Upload className="h-5 w-5" />
          </div>
          <p className="text-sm font-medium">Drop a PAN image or PDF here</p>
          <p className="text-xs text-muted-foreground">JPG, PNG or PDF · max 8 MB · 3 attempts</p>
          <Button variant="outline" size="sm" onClick={() => inputRef.current?.click()} disabled={busy}>
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileText className="h-3.5 w-3.5" />}
            Choose file
          </Button>
          {pendingUpload ? (
            <Button size="sm" onClick={() => inputRef.current?.click()} disabled={busy}>
              Upload for copilot
            </Button>
          ) : null}
        </div>

        {result || document ? (
          <div className="space-y-3 rounded-lg border border-border p-3">
            <div className="flex items-center justify-between text-xs">
              <span className="truncate font-medium">{result?.filename ?? document?.filename ?? "Document"}</span>
              <Badge
                variant={
                  quality?.acceptable || accepted
                    ? "success"
                    : (result?.status ?? document?.status) === "needs_review"
                      ? "warning"
                      : "destructive"
                }
              >
                {quality?.acceptable || accepted ? "acceptable" : result?.status ?? document?.status ?? "unknown"}
              </Badge>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Metric label="Quality" value={quality?.quality_score ?? document?.quality_score} threshold={0.7} />
              <Metric label="OCR confidence" value={quality?.ocr_confidence ?? document?.ocr_confidence} threshold={0.85} />
              <Metric label="Blur" value={quality?.blur_score ?? document?.blur_score} threshold={0.6} />
              <Metric
                label="Rotation"
                value={quality?.rotation_degrees ?? document?.rotation_degrees}
                threshold={5}
                higher={false}
              />
            </div>
            {issues.length ? (
              <div className="space-y-1 rounded-md bg-amber-50 p-2 text-xs text-amber-900">
                {issues.map((issue) => (
                  <p key={issue} className="flex items-start gap-1.5">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> {issue}
                  </p>
                ))}
              </div>
            ) : null}
            <p className="text-xs text-muted-foreground">{result?.message ?? "Previously uploaded document."}</p>
          </div>
        ) : null}
      </CardContent>
      <CardFooter className="justify-between">
        <span className="text-xs text-muted-foreground">The copilot can recommend a retry, never upload silently.</span>
        <div className="flex gap-2">
          {status.document && status.document !== "accepted" ? (
            <Button variant="outline" onClick={() => inputRef.current?.click()}>
              <RotateCcw className="h-4 w-4" /> Retry upload
            </Button>
          ) : null}
          {quality?.acceptable && !accepted ? (
            <Button onClick={accept} disabled={accepting}>
              {accepting ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              Use this extraction
            </Button>
          ) : null}
          {accepted ? <Button onClick={() => nextScreen()}>Continue</Button> : null}
        </div>
      </CardFooter>
    </Card>
  );
}
