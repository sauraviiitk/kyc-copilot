import { create } from "zustand";

import { fetchAddressSuggestions } from "../lib/api";
import type { Activity, DocumentContext, MandateContext, ScreenId } from "../lib/types";
import { SCREEN_ORDER } from "../lib/types";
import { newId } from "../lib/utils";

export interface JourneyValues {
  full_name: string;
  pan: string;
  bank: string;
  ifsc: string;
  account_number: string;
  house_number: string;
  street: string;
  city: string;
  state: string;
  pin: string;
  [key: string]: string;
}

const EMPTY_VALUES: JourneyValues = {
  full_name: "",
  pan: "",
  bank: "",
  ifsc: "",
  account_number: "",
  house_number: "",
  street: "",
  city: "",
  state: "",
  pin: "",
};

interface JourneyStore {
  sessionId: string | null;
  screen: ScreenId;
  values: JourneyValues;
  status: Record<string, string>;
  fieldErrors: Record<string, string>;
  errors: string[];
  document: DocumentContext | null;
  mandate: MandateContext | null;
  highlighted: { field: string; nonce: number } | null;
  uploadSignal: number;
  pendingUpload: { runId: string; actionId: string } | null;
  activity: Activity[];
  stateOptions: string[];
  cityOptions: string[];
  hydrated: boolean;

  setSession: (sessionId: string) => void;
  setScreen: (screen: ScreenId, reason?: string) => void;
  goToStep: (step: number) => void;
  nextScreen: () => void;
  setValue: (name: keyof JourneyValues, value: string) => void;
  setValues: (values: Partial<JourneyValues>) => void;
  setStatus: (key: string, value: string) => void;
  setFieldErrors: (errors: Record<string, string>) => void;
  setErrors: (errors: string[]) => void;
  clearErrors: () => void;
  setDocument: (document: DocumentContext | null) => void;
  setMandate: (mandate: MandateContext | null) => void;
  highlight: (field: string) => void;
  requestUpload: () => void;
  setPendingUpload: (pending: { runId: string; actionId: string } | null) => void;
  addActivity: (tone: Activity["tone"], text: string) => void;
  loadSuggestions: (state?: string) => Promise<void>;
  reset: () => void;
}

export const useJourney = create<JourneyStore>((set, get) => ({
  sessionId: null,
  screen: "kyc.pan_entry",
  values: { ...EMPTY_VALUES },
  status: {},
  fieldErrors: {},
  errors: [],
  document: null,
  mandate: null,
  highlighted: null,
  uploadSignal: 0,
  pendingUpload: null,
  activity: [],
  stateOptions: [],
  cityOptions: [],
  hydrated: false,

  setSession: (sessionId) => set({ sessionId }),

  setScreen: (screen, reason) => {
    set({
      screen,
      fieldErrors: {},
      errors: [],
      highlighted: null,
      hydrated: true,
    });
    if (reason) {
      get().addActivity("system", `Moved to ${screen} (${reason}).`);
    }
  },

  goToStep: (step) => {
    const screen = SCREEN_ORDER[Math.min(Math.max(step - 1, 0), SCREEN_ORDER.length - 1)];
    get().setScreen(screen);
  },

  nextScreen: () => {
    const idx = SCREEN_ORDER.indexOf(get().screen);
    if (idx < SCREEN_ORDER.length - 1) {
      get().setScreen(SCREEN_ORDER[idx + 1]);
    }
  },

  setValue: (name, value) =>
    set((state) => ({
      values: { ...state.values, [name]: value },
      fieldErrors: { ...state.fieldErrors, [name]: "" },
    })),

  setValues: (values) =>
    set((state) => ({ values: { ...state.values, ...values } as JourneyValues })),

  setStatus: (key, value) => set((state) => ({ status: { ...state.status, [key]: value } })),

  setFieldErrors: (fieldErrors) => set({ fieldErrors, errors: Object.values(fieldErrors) }),

  setErrors: (errors) => set({ errors }),

  clearErrors: () => set({ fieldErrors: {}, errors: [] }),

  setDocument: (document) => set({ document }),

  setMandate: (mandate) => set({ mandate }),

  highlight: (field) => set({ highlighted: { field, nonce: Date.now() } }),

  requestUpload: () => set((state) => ({ uploadSignal: state.uploadSignal + 1 })),

  setPendingUpload: (pendingUpload) => set({ pendingUpload }),

  addActivity: (tone, text) =>
    set((state) => ({
      activity: [
        ...state.activity.slice(-80),
        { id: newId(), tone, text, ts: new Date().toISOString() },
      ],
    })),

  loadSuggestions: async (stateName) => {
    try {
      const data = await fetchAddressSuggestions(stateName);
      set({ stateOptions: data.states ?? [], cityOptions: data.cities ?? [] });
    } catch {
      set({ stateOptions: [], cityOptions: [] });
    }
  },

  reset: () =>
    set({
      sessionId: null,
      screen: "kyc.pan_entry",
      values: { ...EMPTY_VALUES },
      status: {},
      fieldErrors: {},
      errors: [],
      document: null,
      mandate: null,
      highlighted: null,
      uploadSignal: 0,
      pendingUpload: null,
      activity: [],
      hydrated: false,
    }),
}));

export function fieldValue(name: string): string {
  return useJourney.getState().values[name] ?? "";
}
