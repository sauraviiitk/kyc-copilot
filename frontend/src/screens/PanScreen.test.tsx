import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { verifyPan } from "../lib/api";
import { useJourney } from "../store/journey";
import { PanScreen } from "./PanScreen";

vi.mock("../lib/api", () => ({
  verifyPan: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : String(error)),
  fetchAddressSuggestions: vi.fn().mockResolvedValue({ states: [], cities: [] }),
}));

const mockedVerifyPan = vi.mocked(verifyPan);

describe("PanScreen", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useJourney.getState().reset();
    useJourney.getState().setSession("session-abc");
  });

  it("uppercases typed PAN characters and gates verification on required fields", () => {
    render(<PanScreen />);
    const panInput = screen.getByLabelText(/^PAN/i) as HTMLInputElement;
    const verifyButton = screen.getByRole("button", { name: /verify pan/i });

    expect(verifyButton).toBeDisabled();
    fireEvent.change(panInput, { target: { value: "abcde1234f" } });
    expect(panInput.value).toBe("ABCDE1234F");
    expect(verifyButton).toBeDisabled(); // name still missing

    fireEvent.change(screen.getByLabelText(/full name/i), { target: { value: "Aarav Sharma" } });
    expect(verifyButton).toBeEnabled();
  });

  it("loads the lowercase demo profile so the agent can fix it", () => {
    render(<PanScreen />);
    fireEvent.click(screen.getByRole("button", { name: /load demo profile/i }));
    expect(useJourney.getState().values.full_name).toBe("Rahul Sharma");
    expect((screen.getByLabelText(/^PAN/i) as HTMLInputElement).value).toBe("abcde1234f");
  });

  it("verifies only through an explicit user action and locks the fields on success", async () => {
    mockedVerifyPan.mockResolvedValue({
      status: "verified",
      message: "PAN verified.",
      attempts: 1,
      remaining_attempts: 2,
      next_screen: "kyc.document_upload",
    });
    render(<PanScreen />);
    act(() => useJourney.getState().setValues({ full_name: "Aarav Sharma", pan: "ABCDE1234F" }));
    const verifyButton = screen.getByRole("button", { name: /verify pan/i });
    await waitFor(() => expect(verifyButton).toBeEnabled());

    fireEvent.click(verifyButton);

    await waitFor(() =>
      expect(mockedVerifyPan).toHaveBeenCalledWith({
        session_id: "session-abc",
        full_name: "Aarav Sharma",
        pan: "ABCDE1234F",
      }),
    );
    expect(useJourney.getState().status.pan).toBe("verified");
    expect(await screen.findByText("Verified")).toBeInTheDocument();
    expect(screen.getByLabelText(/^PAN/i)).toBeDisabled();
    expect(screen.getByRole("button", { name: /continue/i })).toBeInTheDocument();
  });

  it("surfaces structural verification failures without mutating PAN state", async () => {
    mockedVerifyPan.mockResolvedValue({
      status: "failed",
      message: "PAN could not be verified (E-PAN-01).",
      error_code: "pan_lowercase",
      attempts: 1,
      remaining_attempts: 2,
    });
    render(<PanScreen />);
    act(() => useJourney.getState().setValues({ full_name: "Aarav Sharma", pan: "ABCDE1234F" }));
    const verifyButton = screen.getByRole("button", { name: /verify pan/i });
    await waitFor(() => expect(verifyButton).toBeEnabled());

    fireEvent.click(verifyButton);

    expect(await screen.findByText(/PAN format is invalid/i)).toBeInTheDocument();
    expect(useJourney.getState().status.pan).toBe("failed");
    expect(screen.getByLabelText(/^PAN/i)).not.toBeDisabled();
    expect(useJourney.getState().activity.some((item) => item.tone === "error")).toBe(true);
  });
});
