import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchActionValue,
  fetchHealth,
  reportActionResult,
  retryMandate,
  startAgentRun,
} from "../lib/api";
import type { AgentAction } from "../lib/types";
import { useJourney } from "../store/journey";
import { CopilotPanel } from "./CopilotPanel";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchHealth: vi.fn(),
    startAgentRun: vi.fn(),
    fetchActionValue: vi.fn(),
    reportActionResult: vi.fn(),
    retryMandate: vi.fn(),
  };
});

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly url: string;
  readonly handlers = new Map<string, (event: MessageEvent) => void>();
  onerror: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
  }

  addEventListener(name: string, handler: (event: MessageEvent) => void) {
    this.handlers.set(name, handler);
  }

  emit(name: string, data: object) {
    act(() => this.handlers.get(name)?.({ data: JSON.stringify(data) } as MessageEvent));
  }

  close() {
    this.closed = true;
  }
}

const mockFetchHealth = vi.mocked(fetchHealth);
const mockStartAgentRun = vi.mocked(startAgentRun);
const mockFetchActionValue = vi.mocked(fetchActionValue);
const mockReportActionResult = vi.mocked(reportActionResult);
const mockRetryMandate = vi.mocked(retryMandate);

const RUN = {
  run_id: "run-1",
  session_id: "session-abc",
  status: "queued",
  stream_url: "/api/agent/stream/run-1",
};

async function sendMessage() {
  fireEvent.change(screen.getByPlaceholderText(/my PAN is being rejected/i), {
    target: { value: "my PAN is being rejected, help me fix it" },
  });
  fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
  await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
  return MockEventSource.instances[MockEventSource.instances.length - 1];
}

describe("CopilotPanel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    MockEventSource.instances = [];
    localStorage.clear();
    useJourney.getState().reset();
    vi.stubGlobal("EventSource", MockEventSource);
    mockFetchHealth.mockResolvedValue({
      status: "ok",
      provider: "mock",
      model: "deterministic-planner",
      policy_backend: "local",
    });
    mockStartAgentRun.mockResolvedValue(RUN);
    mockFetchActionValue.mockResolvedValue({
      action_id: "act-fill",
      field: "pan",
      value: "ABCDE1234F",
      value_hash: "hash-abc123",
    });
    mockReportActionResult.mockResolvedValue({
      ok: true,
      run_status: "completed",
      action_status: "applied",
    });
  });

  it("gates sending on session, live context and non-empty input", async () => {
    const { rerender } = render(<CopilotPanel contextReady={false} />);
    const textbox = screen.getByPlaceholderText(/my PAN is being rejected/i);
    const sendButton = screen.getByRole("button", { name: /^send$/i });

    expect(sendButton).toBeDisabled();
    fireEvent.change(textbox, { target: { value: "help" } });
    expect(sendButton).toBeDisabled(); // context not ready

    act(() => useJourney.getState().setSession("session-abc"));
    rerender(<CopilotPanel contextReady />);
    expect(sendButton).toBeEnabled();

    fireEvent.click(sendButton);
    await waitFor(() =>
      expect(mockStartAgentRun).toHaveBeenCalledWith({
        session_id: "session-abc",
        message: "help",
        prompt_version: "v2",
      }),
    );
    expect(MockEventSource.instances[0].url).toBe(RUN.stream_url);
  });

  it("listens to every named SSE event and reflects plan, policy and message frames", async () => {
    act(() => useJourney.getState().setSession("session-abc"));
    render(<CopilotPanel contextReady />);
    const source = await sendMessage();

    const expected = [
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
    ];
    expect([...source.handlers.keys()]).toEqual(expected);

    source.emit("agent_plan", { plan: "Normalise PAN case", confidence: 0.93 });
    source.emit("policy", { refs: ["kyc.pan.case_normalisation"] });
    source.emit("agent_message", { say: "I can fix the letter case for you." });

    expect(await screen.findByText("I can fix the letter case for you.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /pii masked/i }));
    expect(screen.getByText("Normalise PAN case")).toBeInTheDocument();
    expect(screen.getByText("kyc.pan.case_normalisation")).toBeInTheDocument();

    source.emit("agent_complete", { status: "completed", confidence: 0.93, degraded: true });
    expect(
      await screen.findByText("The live model was unavailable, so the deterministic planner answered."),
    ).toBeInTheDocument();
    source.emit("stream_end", { reason: "complete" });
    expect(source.closed).toBe(true);
  });

  it("executes a fill via the value endpoint and attests by hash without leaking the value", async () => {
    act(() => useJourney.getState().setSession("session-abc"));
    render(<CopilotPanel contextReady />);
    const source = await sendMessage();

    const action: AgentAction = {
      action_id: "act-fill",
      type: "fill",
      field: "pan",
      preview: "••••••••F",
      reason: "Normalise PAN case",
      sensitive: true,
    };
    source.emit("action", action);

    await waitFor(() =>
      expect(mockFetchActionValue).toHaveBeenCalledWith("act-fill", "session-abc"),
    );
    await waitFor(() => expect(useJourney.getState().values.pan).toBe("ABCDE1234F"));
    await waitFor(() => expect(mockReportActionResult).toHaveBeenCalledTimes(1));

    const payload = mockReportActionResult.mock.calls[0][0];
    expect(payload).toMatchObject({
      run_id: RUN.run_id,
      action_id: "act-fill",
      session_id: "session-abc",
      status: "applied",
      executed_value_hash: "hash-abc123",
    });
    // The raw PAN is fetched separately for the screen input; it never appears
    // in the result payload, activity log or any rendered copilot message.
    expect(JSON.stringify(payload)).not.toContain("ABCDE1234F");
    expect(JSON.stringify(useJourney.getState().activity)).not.toContain("ABCDE1234F");
    expect(screen.getByText(/applied/i)).toBeInTheDocument();
  });

  it("never retries a mandate until the user explicitly confirms", async () => {
    act(() => useJourney.getState().setSession("session-abc"));
    render(<CopilotPanel contextReady />);
    const source = await sendMessage();

    const action: AgentAction = {
      action_id: "act-retry",
      type: "retry",
      reason: "Bank gateway timed out",
      idempotency_key: "idem-123",
    };
    source.emit("action", action);
    source.emit("agent_complete", {
      status: "awaiting_confirmation",
      requires_confirmation: true,
      confirmation_prompt: "Retry the bank mandate now?",
    });

    const confirmButton = await screen.findByRole("button", { name: /confirm retry/i });
    expect(mockRetryMandate).not.toHaveBeenCalled();
    expect(screen.getByText("Retry the bank mandate now?")).toBeInTheDocument();

    mockRetryMandate.mockResolvedValue({
      mandate_ref: "MND-9",
      status: "verified",
      attempts: 2,
      max_attempts: 3,
      message: "Mandate verified.",
      idempotency_key: "idem-123",
      idempotent_replay: false,
      retryable: false,
      requires_confirmation: false,
    });

    fireEvent.click(confirmButton);
    await waitFor(() =>
      expect(mockRetryMandate).toHaveBeenCalledWith("session-abc", "idem-123"),
    );
    await waitFor(() => expect(mockReportActionResult).toHaveBeenCalled());

    expect(mockReportActionResult.mock.calls[0][0]).toMatchObject({
      action_id: "act-retry",
      status: "applied",
      detail: { mandate_ref: "MND-9", attempts: 2 },
    });
    expect(useJourney.getState().mandate?.status).toBe("verified");
  });
});
