export type ScreenId =
  | "kyc.pan_entry"
  | "kyc.document_upload"
  | "kyc.bank_mandate"
  | "kyc.address"
  | "kyc.review";

export const SCREEN_ORDER: ScreenId[] = [
  "kyc.pan_entry",
  "kyc.document_upload",
  "kyc.bank_mandate",
  "kyc.address",
  "kyc.review",
];

export const SCREEN_TITLES: Record<ScreenId, string> = {
  "kyc.pan_entry": "PAN details",
  "kyc.document_upload": "Identity document",
  "kyc.bank_mandate": "Bank mandate",
  "kyc.address": "Address",
  "kyc.review": "Review & finish",
};

export interface FieldContext {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  pattern?: string | null;
  value?: string | null;
  error?: string | null;
  options?: string[] | null;
  sensitive?: boolean;
  disabled?: boolean;
  hint?: string | null;
}

export interface DocumentContext {
  document_id?: string | null;
  filename?: string | null;
  doc_type?: string | null;
  status?: string | null;
  quality_score?: number | null;
  ocr_confidence?: number | null;
  rotation_degrees?: number | null;
  blur_score?: number | null;
  issues?: string[];
}

export interface MandateContext {
  mandate_ref?: string | null;
  status?: string | null;
  attempts?: number;
  max_attempts?: number;
  last_error?: string | null;
  idempotency_key?: string | null;
}

export interface ScreenContext {
  session_id?: string | null;
  screen_id: ScreenId;
  step: number;
  title?: string;
  fields: FieldContext[];
  errors: string[];
  status: Record<string, string>;
  document?: DocumentContext | null;
  mandate?: MandateContext | null;
  captured_at?: string;
}

export type ActionType =
  | "highlight"
  | "fill"
  | "upload"
  | "retry"
  | "navigate"
  | "escalate"
  | "submit";

export interface AgentAction {
  action_id: string;
  type: ActionType;
  field?: string | null;
  target?: string | null;
  step_id?: string | null;
  idempotency_key?: string | null;
  preview?: string | null;
  reason?: string;
  status?: string;
  requires_confirmation?: boolean;
  sensitive?: boolean;
  result?: Record<string, unknown> | null;
}

export interface AgentEvent {
  event: string;
  data: Record<string, any>;
}

export interface SessionResponse {
  session_id: string;
  status: string;
  current_screen_id: ScreenId;
  current_step: number;
  consent_given: boolean;
  created_at: string;
  screen_context?: ScreenContext | null;
  flags: Record<string, unknown>;
}

export interface PANVerifyResponse {
  status: "verified" | "failed";
  message: string;
  error_code?: string | null;
  attempts: number;
  remaining_attempts: number;
  next_screen?: string | null;
  normalized_pan_hint?: string | null;
}

export interface DocumentQuality {
  quality_score: number;
  ocr_confidence: number;
  rotation_degrees: number;
  blur_score: number;
  issues: string[];
  acceptable: boolean;
}

export interface DocumentUploadResponse {
  document_id: string;
  doc_type: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  quality: DocumentQuality;
  ocr: Record<string, any>;
  status: "accepted" | "rejected" | "needs_review";
  message: string;
  requires_user_confirmation: boolean;
}

export interface MandateResponse {
  mandate_ref: string;
  status: "pending" | "verified" | "failed" | "unknown";
  attempts: number;
  max_attempts: number;
  message: string;
  error_code?: string | null;
  idempotency_key: string;
  idempotent_replay: boolean;
  retryable: boolean;
  requires_confirmation: boolean;
}

export interface MandateStatus {
  status: string;
  mandate_ref?: string;
  bank?: string;
  masked_account?: string;
  attempts: number;
  max_attempts: number;
  last_error?: string | null;
  idempotency_key?: string;
  session_id?: string;
  simulated?: boolean;
}

export interface AddressVerifyResponse {
  status: "verified" | "failed";
  message: string;
  field_errors: Record<string, string>;
  suggestions: Record<string, string[]>;
  next_screen?: string | null;
}

export interface ActionValueResponse {
  action_id: string;
  field: string | null;
  value: string;
  value_hash: string | null;
}

export interface ActionResultPayload {
  run_id: string;
  action_id: string;
  session_id: string;
  status: "applied" | "failed" | "rejected" | "cancelled";
  detail?: Record<string, unknown>;
  executed_value_hash?: string | null;
}

export interface HealthResponse {
  status: string;
  provider?: string;
  model?: string;
  policy_backend?: string;
  database?: string;
  kill_switch?: boolean;
  version?: string;
}

export interface Activity {
  id: string;
  tone: "user" | "agent" | "action" | "system" | "error";
  text: string;
  ts: string;
}
