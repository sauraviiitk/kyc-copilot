import { useState } from "react";
import { ArrowRight, BadgeCheck, Loader2, ShieldCheck, Sparkles } from "lucide-react";

import { fieldValue, useJourney } from "../store/journey";
import { errorMessage, verifyPan } from "../lib/api";
import { SCREEN_FIELDS } from "../lib/fields";
import type { PANVerifyResponse } from "../lib/types";
import { cn } from "../lib/utils";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { Field, InlineAlert } from "../components/ui/field";
import { Input } from "../components/ui/input";

export function PanScreen() {
  const { values, setValue, setValues, setStatus, status, setFieldErrors, fieldErrors, highlighted, addActivity, nextScreen } =
    useJourney();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PANVerifyResponse | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const verified = status.pan === "verified";
  const fields = SCREEN_FIELDS["kyc.pan_entry"];

  async function submit() {
    setBusy(true);
    setMessage(null);
    try {
      const response = await verifyPan({
        session_id: useJourney.getState().sessionId ?? "",
        full_name: values.full_name || fieldValue("full_name"),
        pan: values.pan,
      });
      setResult(response);
      if (response.status === "verified") {
        setStatus("pan", "verified");
        setFieldErrors({});
        addActivity("system", "PAN verified. Moving to document upload.");
        window.setTimeout(() => nextScreen(), 650);
      } else {
        setStatus("pan", "failed");
        const errors: Record<string, string> = {};
        if (response.error_code?.includes("pan")) errors.pan = "PAN format is invalid.";
        setFieldErrors(Object.keys(errors).length ? errors : {});
        setMessage(response.message);
        addActivity("error", `PAN verification failed: ${response.message}`);
      }
    } catch (error) {
      setStatus("pan", "failed");
      setMessage(errorMessage(error));
      addActivity("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Verify your PAN</CardTitle>
            <CardDescription>Step 1 of 5 · Identity details are masked before the copilot sees them.</CardDescription>
          </div>
          {verified ? (
            <Badge variant="success">
              <BadgeCheck className="h-3 w-3" /> Verified
            </Badge>
          ) : (
            <Badge variant="outline">Pending</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {message ? (
          <InlineAlert tone={status.pan === "verified" ? "success" : "error"} title="Verification">
            {message}
          </InlineAlert>
        ) : null}

        {fieldErrors.pan ? <InlineAlert tone="error">{fieldErrors.pan}</InlineAlert> : null}

        {fields.map((def) => {
          const Comp = Input;
          return (
            <Field
              key={def.name}
              label={def.label}
              htmlFor={`field-${def.name}`}
              hint={def.hint}
              required={def.required}
              action={
                def.name === "pan" && !values.pan ? (
                  <button
                    type="button"
                    className="text-[11px] font-medium text-primary hover:underline"
                    onClick={() => setValues({ full_name: "Rahul Sharma", pan: "abcde1234f" })}
                  >
                    Load demo profile
                  </button>
                ) : null
              }
            >
              <Comp
                id={`field-${def.name}`}
                data-agent-field={def.name}
                className={cn(
                  highlighted?.field === def.name && "ring-2 ring-primary animate-flash-highlight",
                  fieldErrors[def.name] && "border-rose-400",
                )}
                value={values[def.name as keyof typeof values] ?? ""}
                placeholder={def.placeholder}
                autoComplete={def.autoComplete}
                disabled={verified}
                onChange={(event) =>
                  setValue(def.name as "full_name" | "pan", def.name === "pan" ? event.target.value.toUpperCase() : event.target.value)
                }
                spellCheck={false}
              />
            </Field>
          );
        })}

        <div className="flex items-start gap-2 rounded-lg bg-muted/60 p-3 text-xs text-muted-foreground">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
          <p>
            The copilot can suggest a corrected value, but verification is only ever submitted by you. Lowercase PAN
            characters are a formatting defect the copilot can fix — it can never invent characters.
          </p>
        </div>

        {result && result.status === "failed" ? (
          <p className="text-xs text-muted-foreground">
            Attempt {result.attempts} of 3 · {result.remaining_attempts} remaining before human review.
          </p>
        ) : null}
      </CardContent>
      <CardFooter className="justify-between">
        <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <Sparkles className="h-3.5 w-3.5" /> Ask the copilot if you are stuck.
        </span>
        {verified ? (
          <Button onClick={() => nextScreen()}>
            Continue <ArrowRight className="h-4 w-4" />
          </Button>
        ) : (
          <Button onClick={submit} disabled={busy || !values.pan || !values.full_name}>
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Verify PAN
          </Button>
        )}
      </CardFooter>
    </Card>
  );
}
