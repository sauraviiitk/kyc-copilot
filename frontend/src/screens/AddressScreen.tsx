import { useEffect, useState } from "react";
import { Loader2, MapPin, ShieldCheck } from "lucide-react";

import { errorMessage, verifyAddress } from "../lib/api";
import { useJourney } from "../store/journey";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { Field, InlineAlert } from "../components/ui/field";
import { Input, Select } from "../components/ui/input";

const FALLBACK_STATES = [
  "Andhra Pradesh",
  "Delhi",
  "Gujarat",
  "Haryana",
  "Karnataka",
  "Kerala",
  "Madhya Pradesh",
  "Maharashtra",
  "Punjab",
  "Rajasthan",
  "Tamil Nadu",
  "Telangana",
  "Uttar Pradesh",
  "West Bengal",
];

export function AddressScreen() {
  const {
    sessionId,
    values,
    setValue,
    setStatus,
    status,
    setFieldErrors,
    fieldErrors,
    errors,
    setErrors,
    highlighted,
    stateOptions,
    cityOptions,
    loadSuggestions,
    addActivity,
    nextScreen,
  } = useJourney();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void loadSuggestions(values.state || undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (values.state) void loadSuggestions(values.state);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values.state]);

  async function submit() {
    if (!sessionId) return;
    setBusy(true);
    setMessage(null);
    try {
      const response = await verifyAddress({
        session_id: sessionId,
        house_number: values.house_number,
        street: values.street,
        city: values.city,
        state: values.state,
        pin: values.pin,
      });
      if (response.status === "verified") {
        setStatus("address", "verified");
        setFieldErrors({});
        addActivity("system", "Address verified. Moving to review.");
        window.setTimeout(() => nextScreen(), 600);
      } else {
        setStatus("address", "failed");
        setFieldErrors(response.field_errors ?? {});
        setErrors(Object.values(response.field_errors ?? {}));
        setMessage(response.message);
        addActivity("error", `Address verification failed: ${response.message}`);
        if (response.suggestions?.city) {
          useJourney.setState({ cityOptions: response.suggestions.city });
        }
      }
    } catch (error) {
      setMessage(errorMessage(error));
      addActivity("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const verified = status.address === "verified";
  const states = stateOptions.length ? stateOptions : FALLBACK_STATES;
  const cities = cityOptions;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <div>
            <CardTitle>Confirm your address</CardTitle>
            <CardDescription>Step 4 of 5 · City/state changes must come from the on-screen suggestions.</CardDescription>
          </div>
          <Badge variant={verified ? "success" : status.address === "failed" ? "destructive" : "outline"}>
            {status.address ?? "not started"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {message ? <InlineAlert tone="error">{message}</InlineAlert> : null}
        {errors.length && !message ? (
          <InlineAlert tone="warning">{errors.join(" · ")}</InlineAlert>
        ) : null}

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <Field label="House / flat number" htmlFor="field-house" required error={fieldErrors.house_number}>
            <Input
              id="field-house"
              data-agent-field="house_number"
              className={highlighted?.field === "house_number" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
              value={values.house_number}
              onChange={(event) => setValue("house_number", event.target.value)}
              autoComplete="address-line1"
            />
          </Field>
          <Field label="Street" htmlFor="field-street" required error={fieldErrors.street}>
            <Input
              id="field-street"
              data-agent-field="street"
              className={highlighted?.field === "street" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
              value={values.street}
              onChange={(event) => setValue("street", event.target.value)}
              autoComplete="address-line2"
            />
          </Field>
          <Field
            label="City"
            htmlFor="field-city"
            required
            error={fieldErrors.city}
            hint={cities.length ? "Suggestions are provided by the address service." : undefined}
          >
            <Input
              id="field-city"
              data-agent-field="city"
              list="city-options"
              className={highlighted?.field === "city" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
              value={values.city}
              onChange={(event) => setValue("city", event.target.value)}
              autoComplete="address-level2"
            />
            <datalist id="city-options">
              {cities.map((city) => (
                <option key={city} value={city} />
              ))}
            </datalist>
          </Field>
          <Field label="State" htmlFor="field-state" required error={fieldErrors.state}>
            <Select
              id="field-state"
              data-agent-field="state"
              className={highlighted?.field === "state" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
              value={values.state}
              onChange={(event) => setValue("state", event.target.value)}
              autoComplete="address-level1"
            >
              <option value="">Select a state</option>
              {states.map((state) => (
                <option key={state} value={state}>
                  {state}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="PIN code" htmlFor="field-pin" required error={fieldErrors.pin}>
            <Input
              id="field-pin"
              data-agent-field="pin"
              className={highlighted?.field === "pin" ? "animate-flash-highlight ring-2 ring-primary" : undefined}
              value={values.pin}
              inputMode="numeric"
              onChange={(event) => setValue("pin", event.target.value)}
              autoComplete="postal-code"
            />
          </Field>
        </div>

        <div className="flex items-start gap-2 rounded-lg bg-muted/60 p-3 text-xs text-muted-foreground">
          <MapPin className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
          <p>
            The copilot may remove spaces from a PIN or pick a city from the suggestion list. It will never invent a
            house number or street — those must be typed by you.
          </p>
        </div>

        <p className="inline-flex items-center gap-1 text-[11px] text-muted-foreground">
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-600" /> Address fields are tokenised for the model; the full
          value stays between you and this app.
        </p>
      </CardContent>
      <CardFooter className="justify-between">
        <span className="text-xs text-muted-foreground">Verification is submitted only by you.</span>
        {verified ? (
          <Button onClick={() => nextScreen()}>Continue to review</Button>
        ) : (
          <Button
            onClick={submit}
            disabled={
              busy || !values.house_number || !values.street || !values.city || !values.state || !values.pin
            }
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            Verify address
          </Button>
        )}
      </CardFooter>
    </Card>
  );
}
