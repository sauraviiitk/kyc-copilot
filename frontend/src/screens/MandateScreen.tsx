import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, Landmark, Loader2, RefreshCcw, ShieldAlert } from "lucide-react";

import {
  errorMessage,
  fetchMandateStatus,
  retryMandate,
  submitMandate,
} from "../lib/api";
import { newId } from "../lib/utils";
import type { MandateResponse } from "../lib/types";
import { useJourney } from "../store/journey";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { Field, InlineAlert } from "../components/ui/field";
import { Input } from "../components/ui/input";

export function MandateScreen() {
  const {
    sessionId,
    values,
    setValue,
    setMandate,
    mandate,
    status,
    setStatus,
    setFieldErrors,
    fieldErrors,
    highlighted,
    addActivity,
    nextScreen,
  } = useJourney();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<MandateResponse | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [confirmRetry, setConfirmRetry] = useState(false);

  const keyRef = useMemo(() => ({ current: newId() }), []);

  useEffect(() => {
    if (!sessionId) return;
    fetchMandateStatus(sessionId)
      .then((data) => {
        if (data.status !== "not_started") {
          keyRef.current = data.idempotency_key ?? keyRef.current;
          setMandate({
            mandate_ref: data.mandate_ref ?? null,
            status: data.status,
            attempts: data.attempts,
            max_attempts: data.max_attempts,
            last_error: data.last_error ?? null,
            idempotency_key: data.idempotency_key ?? null,
          });
          setStatus("mandate", data.status);
        }
      })
      .catch(() => undefined);
  }, [sessionId, keyRef, setMandate, setStatus]);

  const current = result ?? null;
  const currentStatus = current?.status ?? mandate?.status ?? status.mandate;
  const attempts = current?.attempts ?? mandate?.attempts ?? 0;
  const lastError = current?.error_code ? current.message : mandate?.last_error;

  function applyOutcome(outcome: MandateResponse) {
    setResult(outcome);
    keyRef.current = outcome.idempotency_key;
    setMandate({
      mandate_ref: outcome.mandate_ref,
      status: outcome.status,
      attempts: outcome.attempts,
      max_attempts: outcome.max_attempts,
      last_error: outcome.status === "verified" ? null : outcome.message,
      idempotency_key: outcome.idempotency_key,
    });
    setStatus("mandate", outcome.status);
    addActivity(
      outcome.status === "verified" ? "system" : "error",
      `Mandate ${outcome.status} (attempt ${outcome.attempts}/${outcome.max_attempts})`,
    );
    setMessage(outcome.message);
    if (outcome.status === "verified") {
      setFieldErrors({});
      window.setTimeout(() => nextScreen(), 600);
    }
  }

  async function submit() {
    if (!sessionId) return;
    setBusy(true);
    setMessage(null);
    try {
      const outcome = await submitMandate({
        session_id: sessionId,
        bank: values.bank,
        ifsc: values.ifsc.toUpperCase(),
        account_number: values.account_number.replace(/\s+/g, ""),
        idempotency_key: keyRef.current,
      });
      applyOutcome(outcome);
    } catch (error) {
      setMessage(errorMessage(error));
      addActivity("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function doRetry() {
    if (!sessionId) return;
    setBusy(true);
    setConfirmRetry(false);
    setMessage(null);
    try {
      const outcome = await retryMandate(sessionId, keyRef.current);
      applyOutcome(outcome);
    } catch (error) {
      setMessage(errorMessage(error));
      addActivity("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const verified = currentStatus === "verified";
  const unknown = currentStatus === "unknown";
  const canRetry = (currentStatus === "failed" || currentStatus === "pending") && attempts < 3;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Set up your bank mandate</CardTitle>
            <CardDescription>Step 3 of 5 · Financial action: retries always need your explicit confirmation.</CardDescription>
          </div>
          <Badge variant={verified ? "success" : unknown ? "warning" : currentStatus ? "destructive" : "outline"}>
            {currentStatus ?? "not started"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {message ? (
          <InlineAlert tone={verified ? "success" : unknown ? "warning" : "error"} title="Mandate status">
            {message}
          </InlineAlert>
        ) : null}

        {lastError && !message ? <InlineAlert tone="warning">{lastError}</InlineAlert> : null}

        {unknown ? (
          <InlineAlert tone="warning" title="Transaction status unknown">
            A debit may already exist. Automatic retries are blocked; please escalate to human support.
          </InlineAlert>
        ) : null}

        {!verified ? (
          <>
            <Field label="Bank" htmlFor="field-bank" required error={fieldErrors.bank}>
              <Input
                id="field-bank"
                data-agent-field="bank"
                className={highlighted?.field === "bank" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
                value={values.bank}
                placeholder="HDFC Bank"
                onChange={(event) => setValue("bank", event.target.value)}
                autoComplete="organization"
              />
            </Field>
            <Field label="IFSC" htmlFor="field-ifsc" required error={fieldErrors.ifsc}>
              <Input
                id="field-ifsc"
                data-agent-field="ifsc"
                className={highlighted?.field === "ifsc" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
                value={values.ifsc}
                placeholder="HDFC0001234"
                onChange={(event) => setValue("ifsc", event.target.value.toUpperCase())}
                spellCheck={false}
              />
            </Field>
            <Field
              label="Account number"
              htmlFor="field-account"
              required
              hint="Sensitive: tokenised before any model call, never written to logs."
              error={fieldErrors.account_number}
            >
              <Input
                id="field-account"
                data-agent-field="account_number"
                className={highlighted?.field === "account_number" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
                value={values.account_number}
                placeholder="9–18 digits"
                inputMode="numeric"
                autoComplete="off"
                onChange={(event) => setValue("account_number", event.target.value.replace(/[^\d\s]/g, ""))}
              />
            </Field>
          </>
        ) : null}

        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="rounded-lg border border-border bg-muted/30 p-2.5">
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Mandate attempts</p>
            <p className="font-semibold">{attempts} / 3</p>
          </div>
          <div className="rounded-lg border border-border bg-muted/30 p-2.5">
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Idempotency key</p>
            <p className="truncate font-mono text-[11px]">{keyRef.current.slice(0, 18)}…</p>
          </div>
        </div>

        {confirmRetry ? (
          <InlineAlert tone="warning" title="Confirm financial retry">
            <p className="mb-2">
              This will reuse the same idempotency key and ask the bank for a new attempt. A previous debit may still
              exist. Confirm only if you want to retry.
            </p>
            <div className="flex gap-2">
              <Button size="sm" variant="destructive" onClick={doRetry} disabled={busy}>
                Yes, retry mandate
              </Button>
              <Button size="sm" variant="outline" onClick={() => setConfirmRetry(false)}>
                Cancel
              </Button>
            </div>
          </InlineAlert>
        ) : null}

        {verified ? (
          <div className="flex items-center gap-2 rounded-lg bg-emerald-50 p-3 text-xs text-emerald-800">
            <Landmark className="h-4 w-4" /> Mandate {current?.mandate_ref ?? mandate?.mandate_ref ?? ""} is verified.
          </div>
        ) : null}

        {unknown ? (
          <InlineAlert tone="warning" title="Human support required">
            <p className="flex items-center gap-1.5">
              <ShieldAlert className="h-3.5 w-3.5" /> Ask the copilot to escalate, or use the support option in the
              copilot panel.
            </p>
          </InlineAlert>
        ) : null}
      </CardContent>
      <CardFooter className="justify-between">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <AlertTriangle className="h-3.5 w-3.5" /> Never retried automatically.
        </span>
        <div className="flex gap-2">
          {canRetry && !confirmRetry ? (
            <Button variant="outline" onClick={() => setConfirmRetry(true)} disabled={busy}>
              <RefreshCcw className="h-4 w-4" /> Retry mandate
            </Button>
          ) : null}
          {verified ? (
            <Button onClick={() => nextScreen()}>Continue</Button>
          ) : (
            <Button onClick={submit} disabled={busy || !values.bank || !values.ifsc || !values.account_number}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
              Submit mandate
            </Button>
          )}
        </div>
      </CardFooter>
    </Card>
  );
}
