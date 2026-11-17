# Security & PII handling

## Rules that are enforced in code

1. The model never receives raw PAN, Aadhaar, account number, phone, email, or
   address. `PIIVault.tokenize_context()` replaces values with deterministic,
   format-preserving surrogates such as `⟦PAN_7299⟧` before a plan request is
   built.
2. Stored `ScreenContext` rows contain surrogates and vault tokens, not raw
   values.
3. Audit logs and structured logs pass through `redact_obj()`, which strips
   known-sensitive keys and masks value previews.
4. A sensitive fill's plaintext exists in exactly two places: the encrypted
   vault column and the browser DOM after the user's own value is rehydrated.
   It is never in SSE, audit, eval reports, or action-result payloads.
5. The agent cannot auto-submit sensitive data. Verify/submit steps require an
   explicit user click, and financial retries require confirmation.
6. The agent cannot invent identity characters (see below).

## Tokenisation

`backend/app/services/pii.py` recognises PAN, Aadhaar, bank account, IFSC, PIN,
phone, email, and address-like values. Each raw value is encrypted with
Fernet using a key derived from `SECRET_KEY` and stored in the vault. A
deterministic surrogate preserves the *format* (letter/digit positions and
length) so the model can reason about structure — e.g. it can see that a PAN is
lowercase or 9 characters long without seeing the real characters.

Surrogates are signed with a keyed HMAC so a model cannot forge a token that
maps to an arbitrary vault entry.

## Fill-value safety

A proposed fill is accepted only when it is provably derived from data the user
already supplied:

- **unchanged surrogate** — the model copied the token verbatim;
- **case transform** — `abcde1234f` → `ABCDE1234F`;
- **separator transform** — `560 001` → `560001`;
- **vault token** — the model emitted a token that resolves to the same entry;
- **literal user value** — the exact string already present in the screen
  context or user message.

`rehydrate_with_proposed_transform()` walks surrogate/proposed/real characters
in lockstep. Any character substitution that is not a pure case change returns
`None`, which the guard records as a hallucination and refuses to apply. This
is why "complete the rest of my PAN, it ends 34F" produces a refusal rather
than a made-up identifier.

Tests: `backend/tests/test_pii.py` proves that raw values absent from the model
payload, that logs contain no raw PAN/account numbers, and that invented
characters are rejected. Frontend test
`frontend/src/components/CopilotPanel.test.tsx` proves the raw PAN is not in
the action-result payload or activity log.

## Mandate safety

- Idempotency keys prevent duplicate mandates (see
  [architecture.md](architecture.md#mandate-idempotency)).
- Attempts are capped; exceeding the cap escalates to a human.
- `unknown` mandate status is escalated rather than blindly retried.
- Status responses mask the account number and never expose the full value.
- Every retry is finite and requires confirmation in the UI.

## Deployment checklist

- Set a long, random `SECRET_KEY` in production; rotating it invalidates the
  vault and surrogate signatures.
- Use `DATABASE_URL` pointing to Supabase/Postgres with TLS (`sslmode=require`).
- Set `ADMIN_TOKEN` before exposing `/admin/*`.
- Keep `DEBUG=false` and restrict `CORS_ORIGINS`.
- Do not commit `.env`; only `.env.example` is tracked.
- Uploaded files under `backend/data/uploads` must be on encrypted storage in
  a real deployment.

## Scope

This is a reference implementation. It includes policy-aware guardrails and
PII tokenisation, but it is not a certified KYC/AML system and does not
integrate with real registries, gateways, or geocoders.
