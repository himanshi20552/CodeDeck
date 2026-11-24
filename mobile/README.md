# CodeDeck Manager Mobile

Separate React Native application for iOS and Android. It uses Expo and TypeScript and connects to the same FastAPI backend as the CodeDeck web application. It does not create a backend, database, or web bundle.

## Requirements

- Node.js 22.13 or newer for the current Expo SDK 58 toolchain.
- npm.
- Expo Go on a physical device, or an Android/iOS simulator. iOS native builds require macOS/Xcode.
- A CodeDeck backend URL reachable from the device. A phone cannot use the developer computer's `localhost`; use a reachable LAN address or HTTPS deployment URL.

## Start the application

From this directory:

```sh
npm install
```

Copy `.env.example` to `.env` and set `EXPO_PUBLIC_API_BASE_URL` to the reachable FastAPI origin (no `/api` suffix). Then start Expo:

```sh
npm start
```

Choose Android or iOS from Expo CLI, or run `npm run android` / `npm run ios`. The backend must be running and reachable from the selected device. Restart Expo after changing the environment file.

Run the TypeScript check with `npm run typecheck`.

## Screens and data

- **Manager dashboard:** live project records, workflow status and elapsed time from `/api/projects` and `/api/projects/{id}/workflow`.
- **Projects and project details:** persisted release workflow, repository snapshot/change summary, analysis results and deployment history.
- **Approval inbox and release review:** uses Phase 5 `/api/approvals/pending`, `/api/approvals/{id}` and `/api/approvals/history` and displays persisted release data.
- **Approval actions:** asks for confirmation, requires a reason to reject, posts to `/api/approvals/{id}/approve` or `/reject`, then displays the backend response. The app never supplies a shared backend credential.
- **Deployment monitoring:** reads `/api/monitoring`, including reported rollout, pod, HPA, metrics, health and alerts data. Missing metrics are shown as unavailable.
- **Activity:** displays persisted monitoring alerts, deployment events, file activity and approval history. While the app is foregrounded, it polls for pending approvals every 30 seconds and shows an in-app alert plus an inbox banner; OS push notifications are not configured.

## Authentication status

The current backend has no end-user or manager identity provider. In normal mode, approval reads/decisions require a shared `RELEASE_APPROVAL_TOKEN`, while approver identity is a caller-supplied, explicitly unverified label. That backend credential is a server secret and **must not be included in this app, an Expo public environment variable, or a distributed mobile binary**. This client includes an authentication adapter seam (`src/api/auth.ts`) for a future OIDC/JWT session provider; no fake login or locally assigned role is used.

For a remote staging demonstration, the backend may enable `RELEASE_APPROVAL_DEMO_MODE=true` only alongside `CODEDECK_ENV=development`; `DEPLOYMENT_TARGET=remote` is supported. The inbox labels this mode. Approval/rejection events are persisted with the fixed identity `Demo Manager (demo)`, marked unverified, and do not enqueue production deployment. See `docs/release-approval-auth.md`; never enable this mode on a publicly accessible shared or production server. With demo mode off and no real user session, the backend denies approval access and the app shows the actual API error.

The API client currently calls project, workflow, analysis, deployment and monitoring reads that the existing backend exposes without user authentication. Deploy this mobile app only in an environment where those backend endpoints are appropriately protected at the network/gateway boundary.
