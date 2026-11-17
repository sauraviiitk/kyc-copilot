import { useState } from "react";
import { BadgeCheck, CheckCircle2, Loader2, ShieldCheck } from "lucide-react";

import { completeJourney, errorMessage } from "../lib/api";
import { maskPreview } from "../lib/utils";
import { useJourney } from "../store/journey";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { InlineAlert } from "../components/ui/field";

function Row({ label, value, ok }: { label: string; value: string; ok?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/70 py-2 text-sm last:border-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="flex items-center gap-2 font-medium">
        {value}
        {ok ? <BadgeCheck className="h-3.5 w-3.5 text-emerald-600" /> : null}
      </span>
    </div>
  );
}

export function ReviewScreen() {
  const { sessionId, values, status, mandate, setStatus, addActivity, goToStep } = useJourney();
  const [busy, setBusy] = useState(false);
  const [finished, setFinished] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function finish() {
    if (!sessionId) return;
    setBusy(true);
    setMessage(null);
    try {
      await completeJourney(sessionId);
      setFinished(true);
      setStatus("journey", "completed");
      addActivity("system", "KYC journey completed by the user.");
    } catch (error) {
      setMessage(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  if (finished) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-14 text-center">
          <div className="rounded-full bg-emerald-100 p-3 text-emerald-700">
            <CheckCircle2 className="h-7 w-7" />
          </div>
          <h2 className="text-lg font-semibold">KYC journey complete</h2>
          <p className="max-w-sm text-sm text-muted-foreground">
            Your PAN, document, bank mandate and address are verified. A confirmation has been recorded in the audit
            trail.
          </p>
          <Badge variant="success">completed</Badge>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Review your KYC</CardTitle>
            <CardDescription>Step 5 of 5 · Sensitive values stay masked on this screen.</CardDescription>
          </div>
          <Badge variant="outline">review</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {message ? <InlineAlert tone="error">{message}</InlineAlert> : null}

        <div className="rounded-lg border border-border px-3 py-1">
          <Row label="Full name" value={values.full_name ? `${values.full_name.slice(0, 2)}••••` : "—"} ok={status.pan === "verified"} />
          <Row label="PAN" value={maskPreview(values.pan)} ok={status.pan === "verified"} />
          <Row label="Document" value={status.document ?? "—"} ok={status.document === "accepted"} />
          <Row label="Bank mandate" value={mandate?.mandate_ref ?? status.mandate ?? "—"} ok={status.mandate === "verified"} />
          <Row label="Address" value={`${values.city || "—"}, ${values.state || "—"}`} ok={status.address === "verified"} />
          <Row label="PIN" value={maskPreview(values.pin, 2)} ok={status.address === "verified"} />
        </div>

        <div className="flex items-start gap-2 rounded-lg bg-muted/60 p-3 text-xs text-muted-foreground">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
          <p>
            Finishing the journey is your explicit confirmation. The copilot cannot submit this step on your behalf.
          </p>
        </div>
      </CardContent>
      <CardFooter className="justify-between">
        <Button variant="ghost" size="sm" onClick={() => goToStep(1)}>
          Back to PAN
        </Button>
        <Button onClick={finish} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
          Finish KYC
        </Button>
      </CardFooter>
    </Card>
  );
}
