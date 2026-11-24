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
- **Approval inbox and release review:** uses Phase 5 `/api/approvals/pending` and `/api/approvals/{id}` and displays the persisted review summary.
- **Approval actions:** posts the confirmed decision to `/api/approvals/{id}/approve` or `/reject`; server responses and errors are shown. Decisions are unavailable until real manager authentication is connected.
- **Deployment monitoring:** reads `/api/monitoring`, including reported rollout, pod, HPA, metrics, health and alerts data. Missing metrics are shown as unavailable.
- **Activity:** displays persisted monitoring alerts, deployment events and file activity. It is an in-app feed; push notifications are not configured.

## Authentication status

The current backend has no end-user or manager identity provider. Phase 5 approval reads/decisions require a shared `RELEASE_APPROVAL_TOKEN`, while approver identity is a caller-supplied, explicitly unverified label. That backend credential is a server secret and **must not be included in this app, an Expo public environment variable, or a distributed mobile binary**. This client includes an authentication adapter seam (`src/api/auth.ts`) for a future OIDC/JWT session provider; no fake login or locally assigned role is used. Until the backend supports real user authentication and authorization, approval requests cannot be read or submitted from this app.

The API client currently calls project, workflow, analysis, deployment and monitoring reads that the existing backend exposes without user authentication. Deploy this mobile app only in an environment where those backend endpoints are appropriately protected at the network/gateway boundary.
