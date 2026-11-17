import type { FieldContext, ScreenContext, ScreenId } from "./types";

export interface FieldDef {
  name: string;
  label: string;
  type?: string;
  required?: boolean;
  pattern?: string;
  placeholder?: string;
  autoComplete?: string;
  hint?: string;
  numeric?: boolean;
}

export const SCREEN_FIELDS: Record<ScreenId, FieldDef[]> = {
  "kyc.pan_entry": [
    {
      name: "full_name",
      label: "Full name (as on PAN)",
      placeholder: "Rahul Sharma",
      autoComplete: "name",
      hint: "Sensitive: masked before it reaches the model.",
    },
    {
      name: "pan",
      label: "PAN",
      placeholder: "ABCDE1234F",
      pattern: "^[A-Z]{5}[0-9]{4}[A-Z]$",
      autoComplete: "off",
      hint: "10 characters: 5 letters, 4 digits, 1 letter.",
    },
  ],
  "kyc.document_upload": [],
  "kyc.bank_mandate": [
    { name: "bank", label: "Bank", placeholder: "HDFC Bank", autoComplete: "organization" },
    { name: "ifsc", label: "IFSC", placeholder: "HDFC0001234", pattern: "^[A-Z]{4}0[A-Z0-9]{6}$" },
    {
      name: "account_number",
      label: "Account number",
      placeholder: "9–18 digits",
      autoComplete: "off",
      hint: "Sensitive: tokenised and never logged.",
      numeric: true,
    },
  ],
  "kyc.address": [
    { name: "house_number", label: "House / flat number", placeholder: "12B", autoComplete: "address-line1" },
    { name: "street", label: "Street", placeholder: "MG Road", autoComplete: "address-line2" },
    { name: "city", label: "City", placeholder: "Bengaluru", autoComplete: "address-level2" },
    { name: "state", label: "State", placeholder: "Karnataka", autoComplete: "address-level1" },
    { name: "pin", label: "PIN code", placeholder: "560001", pattern: "^[1-9][0-9]{5}$", autoComplete: "postal-code" },
  ],
  "kyc.review": [],
};

export const SCREEN_STEP: Record<ScreenId, number> = {
  "kyc.pan_entry": 1,
  "kyc.document_upload": 2,
  "kyc.bank_mandate": 3,
  "kyc.address": 4,
  "kyc.review": 5,
};

export interface ContextInput {
  sessionId: string;
  screen: ScreenId;
  values: Record<string, string>;
  fieldErrors: Record<string, string>;
  errors: string[];
  status: Record<string, string>;
  document?: ScreenContext["document"];
  mandate?: ScreenContext["mandate"];
  stateOptions: string[];
  cityOptions: string[];
}

const OPTION_SOURCES: Partial<Record<string, () => keyof Pick<ContextInput, "stateOptions" | "cityOptions">>> = {
  state: () => "stateOptions",
  city: () => "cityOptions",
};

export function buildScreenContext(input: ContextInput): ScreenContext {
  const fields: FieldContext[] = SCREEN_FIELDS[input.screen].map((def) => {
    const optionKey = OPTION_SOURCES[def.name]?.();
    const options = optionKey ? input[optionKey] : undefined;
    return {
      name: def.name,
      label: def.label,
      type: def.type ?? "text",
      required: def.required ?? true,
      pattern: def.pattern ?? null,
      value: input.values[def.name] ?? "",
      error: input.fieldErrors[def.name] ?? null,
      options: options && options.length ? options : null,
      sensitive: false,
      disabled: false,
      hint: def.hint ?? null,
    };
  });

  return {
    session_id: input.sessionId,
    screen_id: input.screen,
    step: SCREEN_STEP[input.screen],
    title: input.screen,
    fields,
    errors: [...Object.values(input.fieldErrors), ...input.errors].filter(Boolean),
    status: input.status,
    document: input.screen === "kyc.document_upload" ? input.document ?? null : null,
    mandate: input.screen === "kyc.bank_mandate" ? input.mandate ?? null : null,
    captured_at: new Date().toISOString(),
  };
}
