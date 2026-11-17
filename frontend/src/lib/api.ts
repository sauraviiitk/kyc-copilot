import axios from "axios";

import type {
  ActionResultPayload,
  ActionValueResponse,
  AddressVerifyResponse,
  DocumentUploadResponse,
  HealthResponse,
  MandateResponse,
  MandateStatus,
  PANVerifyResponse,
  ScreenContext,
  SessionResponse,
} from "./types";

export const api = axios.create({
  baseURL: "/api",
  timeout: 30000,
  headers: { "Content-Type": "application/json" },
});

export function errorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail.message === "string") return detail.message;
    if (typeof error.message === "string") return error.message;
  }
  return error instanceof Error ? error.message : "Something went wrong";
}

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await axios.get<HealthResponse>("/health");
  return data;
}

export async function createSession(externalRef?: string): Promise<SessionResponse> {
  const { data } = await api.post<SessionResponse>("/sessions", {
    external_ref: externalRef ?? null,
    consent_given: true,
  });
  return data;
}

export async function getSession(sessionId: string): Promise<SessionResponse> {
  const { data } = await api.get<SessionResponse>(`/sessions/${sessionId}`);
  return data;
}

export async function fetchScreenContext(sessionId: string): Promise<ScreenContext | null> {
  try {
    const { data } = await api.get("/screen-context", { params: { session_id: sessionId } });
    return (data.context ?? null) as ScreenContext | null;
  } catch {
    return null;
  }
}

export async function publishScreenContext(
  sessionId: string,
  context: ScreenContext,
  reason = "field_change",
): Promise<void> {
  await api.post("/screen-context/update", {
    session_id: sessionId,
    context,
    reason,
  });
}

export async function startAgentRun(payload: {
  session_id: string;
  message: string;
  prompt_version: "v1" | "v2";
}): Promise<{ run_id: string; session_id: string; status: string; stream_url: string }> {
  const { data } = await api.post("/agent/run", payload);
  return data;
}

export async function reportActionResult(payload: ActionResultPayload): Promise<{
  ok: boolean;
  run_status: string;
  action_status: string;
}> {
  const { data } = await api.post("/agent/action-result", payload);
  return data;
}

export async function fetchActionValue(actionId: string, sessionId: string): Promise<ActionValueResponse> {
  const { data } = await api.get<ActionValueResponse>(`/agent/actions/${actionId}/value`, {
    params: { session_id: sessionId },
  });
  return data;
}

export async function verifyPan(payload: {
  session_id: string;
  full_name: string;
  pan: string;
}): Promise<PANVerifyResponse> {
  const { data } = await api.post<PANVerifyResponse>("/kyc/pan/verify", payload);
  return data;
}

export async function uploadDocument(
  sessionId: string,
  file: File,
  docType = "pan",
): Promise<DocumentUploadResponse> {
  const form = new FormData();
  form.append("session_id", sessionId);
  form.append("doc_type", docType);
  form.append("file", file);
  const { data } = await api.post<DocumentUploadResponse>("/kyc/document/upload", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 60000,
  });
  return data;
}

export async function acceptDocument(sessionId: string, documentId: string): Promise<void> {
  await api.post("/kyc/document/accept", {
    session_id: sessionId,
    document_id: documentId,
    confirm: true,
  });
}

export async function submitMandate(payload: {
  session_id: string;
  bank: string;
  ifsc: string;
  account_number: string;
  idempotency_key: string;
}): Promise<MandateResponse> {
  const { data } = await api.post<MandateResponse>("/kyc/mandate/submit", payload);
  return data;
}

export async function retryMandate(sessionId: string, idempotencyKey: string): Promise<MandateResponse> {
  const { data } = await api.post<MandateResponse>("/kyc/mandate/retry", {
    session_id: sessionId,
    idempotency_key: idempotencyKey,
  });
  return data;
}

export async function fetchMandateStatus(sessionId: string): Promise<MandateStatus> {
  const { data } = await api.get<MandateStatus>(`/kyc/mandate/${sessionId}`);
  return data;
}

export async function verifyAddress(payload: {
  session_id: string;
  house_number: string;
  street: string;
  city: string;
  state: string;
  pin: string;
}): Promise<AddressVerifyResponse> {
  const { data } = await api.post<AddressVerifyResponse>("/kyc/address/verify", payload);
  return data;
}

export async function fetchAddressSuggestions(state?: string): Promise<{
  states: string[];
  cities: string[];
}> {
  const { data } = await api.get("/kyc/suggestions", { params: { state: state || undefined } });
  return data;
}

export async function completeJourney(sessionId: string): Promise<void> {
  await api.patch(`/sessions/${sessionId}`, { status: "completed" });
}
