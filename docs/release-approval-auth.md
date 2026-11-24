# Release approval API security and remote staging demo

The shared FastAPI backend exposes release approval endpoints for a separate manager client. Configure `RELEASE_APPROVAL_TOKEN` with a high-entropy secret in the backend's secret store. Approval reads and decisions require `Authorization: Bearer <token>`. When the setting is empty, those endpoints return `503` and cannot accept decisions.

For a remote staging demonstration, configure the backend with:

```env
DEPLOYMENT_TARGET=remote
LOCAL_KIND_ENABLED=false
CODEDECK_ENV=development
RELEASE_APPROVAL_DEMO_MODE=true
RELEASE_APPROVAL_TOKEN=
REMOTE_WORKER_URL=<backend-url-reachable-from-EC2>
REMOTE_WORKER_TOKEN=<worker-secret>
```

`RELEASE_APPROVAL_DEMO_MODE` is off by default. The development environment restriction remains mandatory; demo mode is rejected in production and test environments. `DEPLOYMENT_TARGET` selects the deployment path and may be `remote` for EC2/K3s staging. It does not authorize production deployment. Remote staging is still queued to and executed by the authenticated EC2 worker. After successful rollout and persisted passing smoke verification, a developer may submit that staging deployment for approval. The manager inbox can then list the request and record an approve or reject decision.

Demo decisions are simulated for demonstration, but persist in the existing approval tables and audit history with the fixed actor `Demo Manager (demo)`, a visible `[DEMO MODE]` comment, and `identity_verified=false`. An approved demo release remains at `Approved`: it does not enqueue a production deployment, create a production deployment record, access Kubernetes for promotion, or claim a real manager identity. Remote production promotion is not implemented. Never enable demo mode on a publicly accessible shared or production backend. Turn the flag off after the demonstration.

`REMOTE_WORKER_TOKEN` authenticates the EC2 worker when it claims staging jobs and reports results. `RELEASE_APPROVAL_TOKEN` is a separate credential for normal authenticated manager API access. Never reuse one token for the other purpose. In normal mode, approval reads and decisions require a high-entropy `RELEASE_APPROVAL_TOKEN`; the token is not sent to React or React Native. The demo bypass is available only under the development guard above.

To run this mode with the existing Compose setup, set these values in your private `.env` file (do not commit it) and restart only the backend. Configure `REMOTE_WORKER_URL` to the API address reachable from EC2, and keep `REMOTE_WORKER_TOKEN` aligned with the worker's secret. Deploy a test project to remote staging and wait for a successful worker completion with a recorded passing smoke result. In the web Release Workflow, submit that staging deployment for approval. Point the mobile app's `EXPO_PUBLIC_API_BASE_URL` at the same backend using an address reachable from the device; the inbox displays a demo banner, and an open app polls for new requests.

CodeDeck currently has no end-user authentication, identity provider integration, or manager role authorization. `X-Approver-Identity` and `X-Requester-Identity` are caller-supplied audit labels, not verified identities; persisted records explicitly mark them unverified. The shared token identifies access to the approval API, not a human approver. Do not embed this token in a distributed mobile application. Until real user authentication and per-user authorization are added, expose approval APIs only through a trusted server-side gateway that authenticates managers and securely injects the token. The backend itself cannot distinguish managers from other holders of the shared token.

Each request, decision, and production transition is persisted in `release_approval_events`. Project deletion is blocked when approval history exists so audit records are retained. The legacy project production-approve endpoint is disabled and cannot switch traffic.

Remote EC2/K3s production rollout and promotion are not implemented. Remote staging approval requests and demo decisions are available; normal authenticated approval that would proceed to production remains unavailable for the remote target. The old project production-approve endpoint is disabled; approval decisions are recorded only through the audited release approval routes.
