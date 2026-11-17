import { describe, expect, it } from "vitest";

import { SCREEN_FIELDS, buildScreenContext, type ContextInput } from "./fields";

function baseInput(overrides: Partial<ContextInput> = {}): ContextInput {
  return {
    sessionId: "session-123",
    screen: "kyc.pan_entry",
    values: {},
    fieldErrors: {},
    errors: [],
    status: {},
    stateOptions: [],
    cityOptions: [],
    ...overrides,
  };
}

describe("buildScreenContext", () => {
  it("publishes live form values for the active screen only", () => {
    const context = buildScreenContext(
      baseInput({
        values: { full_name: "Aarav Sharma", pan: "ABCDE1234F", city: "Mumbai" },
        status: { pan: "failed" },
        fieldErrors: { pan: "PAN could not be verified." },
        errors: ["Session warning"],
      }),
    );

    expect(context.session_id).toBe("session-123");
    expect(context.screen_id).toBe("kyc.pan_entry");
    expect(context.step).toBe(1);
    expect(context.fields.map((field) => field.name)).toEqual(
      SCREEN_FIELDS["kyc.pan_entry"].map((field) => field.name),
    );
    expect(context.fields.map((field) => field.value)).toEqual(["Aarav Sharma", "ABCDE1234F"]);
    expect(context.fields[0].error).toBeNull();
    expect(context.fields[1].error).toBe("PAN could not be verified.");
    expect(context.fields[1].pattern).toBe("^[A-Z]{5}[0-9]{4}[A-Z]$");
    expect(context.errors).toEqual(["PAN could not be verified.", "Session warning"]);
    expect(context.status).toEqual({ pan: "failed" });
    // A value belonging to another screen is never leaked into this snapshot.
    expect(JSON.stringify(context)).not.toContain("Mumbai");
    expect(context.document).toBeNull();
    expect(context.mandate).toBeNull();
    expect(context.captured_at).toBeTruthy();
  });

  it("attaches live suggestion options to state and city fields", () => {
    const context = buildScreenContext(
      baseInput({
        screen: "kyc.address",
        values: { city: "Bengaluru", state: "Karnataka" },
        stateOptions: ["Karnataka", "Maharashtra"],
        cityOptions: ["Bengaluru", "Mysuru"],
      }),
    );

    expect(context.step).toBe(4);
    const state = context.fields.find((field) => field.name === "state");
    const city = context.fields.find((field) => field.name === "city");
    expect(state?.options).toEqual(["Karnataka", "Maharashtra"]);
    expect(city?.options).toEqual(["Bengaluru", "Mysuru"]);
    expect(context.fields.find((field) => field.name === "pan")).toBeUndefined();
  });

  it("cludes document and mandate context only on their own screens", () => {
    const document = { document_id: "doc-1", status: "needs_review", ocr_confidence: 0.42, issues: ["blur"] };
    const mandate = { mandate_ref: "MND-1", status: "failed", attempts: 1, max_attempts: 3 };

    const upload = buildScreenContext(
      baseInput({ screen: "kyc.document_upload", document, mandate }),
    );
    expect(upload.document).toEqual(document);
    expect(upload.mandate).toBeNull();

    const bank = buildScreenContext(baseInput({ screen: "kyc.bank_mandate", document, mandate }));
    expect(bank.document).toBeNull();
    expect(bank.mandate).toEqual(mandate);

    const review = buildScreenContext(baseInput({ screen: "kyc.review", document, mandate }));
    expect(review.document).toBeNull();
    expect(review.mandate).toBeNull();
  });

  it("normalises missing values and empty option lists", () => {
    const context = buildScreenContext(baseInput({ screen: "kyc.bank_mandate" }));
    expect(context.fields.every((field) => field.value === "")).toBe(true);
    expect(context.fields.every((field) => field.options === null)).toBe(true);
    expect(context.errors).toEqual([]);
  });
});
