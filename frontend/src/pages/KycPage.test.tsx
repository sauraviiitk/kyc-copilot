import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  createSession,
  fetchAddressSuggestions,
  fetchHealth,
  publishScreenContext,
} from "../lib/api";
import { useJourney } from "../store/journey";
import { KycPage } from "./KycPage";

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchHealth: vi.fn(),
    createSession: vi.fn(),
    getSession: vi.fn(),
    publishScreenContext: vi.fn(),
    fetchAddressSuggestions: vi.fn(),
  };
});

const SESSION = {
  session_id: "session-strict",
  status: "active",
  current_screen_id: "kyc.pan_entry" as const,
  current_step: 1,
  consent_given: true,
  created_at: "2026-01-01T00:00:00Z",
  screen_context: null,
  flags: {},
};

describe("KycPage boot under React StrictMode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    useJourney.getState().reset();
    vi.mocked(fetchHealth).mockResolvedValue({
      status: "ok",
      provider: "gemini",
      model: "gemini-3.1-flash-lite",
      policy_backend: "local-bm25",
    });
    vi.mocked(createSession).mockResolvedValue(SESSION);
    vi.mocked(publishScreenContext).mockResolvedValue(undefined);
    vi.mocked(fetchAddressSuggestions).mockResolvedValue({ states: [], cities: [] });
  });

  it("clears the start-up spinner after the StrictMode double mount", async () => {
    render(
      <StrictMode>
        <MemoryRouter>
          <KycPage />
        </MemoryRouter>
      </StrictMode>,
    );

    await waitFor(() => expect(useJourney.getState().sessionId).toBe("session-strict"));
    await waitFor(() =>
      expect(screen.queryByText(/Starting an isolated KYC session/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/PAN details/i)).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText(/live context published/i)).toBeInTheDocument(),
    );
  });
});
