# Langfuse

CodeDeck uses the Langfuse Python SDK v5 at the server-side AI boundary. Repository Q&A creates a trace span with project ID and bounded retrieval metadata; the actual question, snippets, prompts and answer are omitted by default. Generation traces include provider/model labels and response token counts when the provider returns usage data. LLM request outcomes and latency are also recorded as Prometheus metrics. Credentials and prompts are never sent from the browser.

## Configuration

Set these variables in the local `.env` or deployment secret store:

```dotenv
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://cloud.langfuse.com
LANGFUSE_CAPTURE_CONTENT=false
```

CodeDeck's status endpoint reports `disabled`, `error` (credentials present but SDK absent), or `configured_unverified`. It does not claim network connectivity from the presence of keys. The integration is optional: missing keys or SDK/network errors do not fail Q&A. Safe warnings record the exception class.

To allow content capture in a trusted development environment only, set `LANGFUSE_CAPTURE_CONTENT=true`. Captured text is truncated and common API-key, token, password and bearer assignments are redacted. Do not enable content capture for sensitive/private repositories without an approved data-handling decision. The default is off.

## Evaluation

Use `cd backend; ..\.venv\Scripts\python.exe scripts/evaluate_retrieval.py` for a deterministic local retrieval evaluation without Langfuse credentials. It reports objective retrieval metrics separately from any LLM judge. The current application does not claim a groundedness score or Langfuse dataset evaluation; those require a reviewed evaluation dataset and are future work.

## Troubleshooting

- `disabled`: set both public and secret keys; restart backend.
- `error`: install dependencies with `pip install -r requirements.txt`.
- `configured_unverified` but no trace appears: check the base URL, provider credentials, network egress and Langfuse server version (SDK v5 requires a compatible current server).
- A trace export failure should be visible as a safe warning in backend logs and must not change the Q&A response.
