/**
 * Tiny fetch wrapper for the backend — React Native edition.
 *
 * Mirrors the web app's `services/http.ts` so the auth/api layers are
 * line-for-line compatible, with two RN-specific differences:
 *   - Base URL comes from `config.ts` (no `import.meta.env`).
 *   - The JWT is persisted with AsyncStorage. Because `apiFetch` needs the
 *     token synchronously, we keep an in-memory copy and hydrate it once at
 *     app start via `tokenStorage.hydrate()`.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';

import { API_BASE_URL } from '../config';

const TOKEN_KEY = 'skyai.authToken';

const buildUrl = (path: string): string => {
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  const origin = API_BASE_URL.replace(/\/+$/, '');
  return path.startsWith('/') ? `${origin}${path}` : `${origin}/${path}`;
};

export class ApiError extends Error {
  status: number;
  code?: string;
  hint?: string;
  detail?: string;

  constructor(
    message: string,
    opts: { status: number; code?: string; hint?: string; detail?: string },
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = opts.status;
    this.code = opts.code;
    this.hint = opts.hint;
    this.detail = opts.detail;
  }
}

let memoryToken: string | null = null;

export const tokenStorage = {
  /** Load the persisted token into memory. Call once at app startup. */
  async hydrate(): Promise<void> {
    try {
      memoryToken = await AsyncStorage.getItem(TOKEN_KEY);
    } catch {
      memoryToken = null;
    }
  },
  get(): string | null {
    return memoryToken;
  },
  set(token: string | null): void {
    memoryToken = token;
    // Persist in the background; failures here don't block the request flow.
    if (token) {
      void AsyncStorage.setItem(TOKEN_KEY, token).catch(() => {});
    } else {
      void AsyncStorage.removeItem(TOKEN_KEY).catch(() => {});
    }
  },
};

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE' | 'PUT';
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
  /** Don't attach the auth token even if one is stored. */
  noAuth?: boolean;
}

const friendlyMessage = (code: string | undefined): string => {
  switch (code) {
    case 'invalid_credentials':
      return 'Email or password is incorrect.';
    case 'email_already_registered':
      return 'An account with this email already exists.';
    case 'password_too_short':
      return 'Password must be at least 8 characters.';
    case 'invalid_email':
      return 'Please enter a valid email address.';
    case 'missing_token':
    case 'invalid_or_expired_token':
      return 'Your session has expired. Please sign in again.';
    case 'admin_only':
      return 'This action requires an admin account.';
    case 'incorrect_current_password':
      return 'Current password is incorrect.';
    case 'twilio_not_configured':
      return 'Calling is not configured yet on the backend.';
    case 'agent_not_registered':
      return "You're not registered as an agent yet. Try logging out and back in.";
    case 'outbound_failed':
      return "Couldn't place the call. Check the destination number.";
    case 'call_not_found':
      return 'That call has already ended.';
    case 'email_not_verified':
      return 'Please verify your email to continue.';
    default:
      return code
        ? code.replace(/_/g, ' ')
        : 'Something went wrong. Please try again.';
  }
};

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(options.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    ...(options.headers ?? {}),
  };

  if (!options.noAuth) {
    const token = tokenStorage.get();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  const body =
    options.body !== undefined ? JSON.stringify(options.body) : undefined;

  let response: Response;
  try {
    response = await fetch(buildUrl(path), {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
    });
  } catch (err) {
    throw new ApiError('Could not reach the server. Check your connection.', {
      status: 0,
      detail: String(err),
    });
  }

  let data: any = null;
  const text = await response.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }

  if (!response.ok) {
    const code = data?.error;
    throw new ApiError(friendlyMessage(code), {
      status: response.status,
      code,
      hint: data?.hint,
      detail: data?.detail,
    });
  }

  return (data ?? {}) as T;
}
