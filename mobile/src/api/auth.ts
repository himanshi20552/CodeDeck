/**
 * Authentication adapter seam for a future OIDC/JWT integration.
 * The backend currently has only a shared service token. It is intentionally
 * not accepted, bundled, or persisted by this mobile application.
 */
export type AuthSession = { accessToken: string; approverIdentity?: string };
export type AccessTokenProvider = () => Promise<AuthSession | null>;

let provider: AccessTokenProvider | null = null;

export function configureAccessTokenProvider(next: AccessTokenProvider | null) {
  provider = next;
}

export function hasAuthProvider() {
  return provider !== null;
}

export async function getAuthSession(): Promise<AuthSession | null> {
  return provider ? provider() : null;
}

export const AUTH_NOT_CONFIGURED =
  "Manager sign-in is not connected. The backend currently uses a shared service token, which must not be placed in a mobile app. Ask your CodeDeck administrator to configure user authentication.";
