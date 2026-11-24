# Release approval API security

The shared FastAPI backend exposes release approval endpoints for a separate manager client. Configure `RELEASE_APPROVAL_TOKEN` with a high-entropy secret in the backend's secret store. Approval reads and decisions require `Authorization: Bearer <token>`. When the setting is empty, those endpoints return `503` and cannot accept decisions.

CodeDeck currently has no end-user authentication, identity provider integration, or manager role authorization. `X-Approver-Identity` and `X-Requester-Identity` are caller-supplied audit labels, not verified identities; persisted records explicitly mark them unverified. The shared token identifies access to the approval API, not a human approver. Do not embed this token in a distributed mobile application. Until real user authentication and per-user authorization are added, expose approval APIs only through a trusted server-side gateway that authenticates managers and securely injects the token. The backend itself cannot distinguish managers from other holders of the shared token.

Each request, decision, and production transition is persisted in `release_approval_events`. Project deletion is blocked when approval history exists so audit records are retained. The legacy project production-approve endpoint is disabled and cannot switch traffic.

The current production blue-green workflow operates on local Kind only. Remote EC2/K3s production rollout and promotion are not implemented; remote approval submission and approval decisions are consequently unavailable while the backend is configured for remote production.
