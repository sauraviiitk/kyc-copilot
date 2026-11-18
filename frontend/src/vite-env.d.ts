/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Backend origin used when the UI is deployed separately from the API.
   * Example: https://kyc-copilot-api.onrender.com
   * Leave unset for same-origin deployments (Docker/nginx, Vite dev proxy).
   */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
