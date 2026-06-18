/**
 * Sky Voice AI — Auth service (React Native).
 *
 * Backend-backed authentication against /api/v1/auth/* on the Flask backend,
 * mirroring the web app's `services/auth.ts`. The JWT lives in `tokenStorage`
 * (AsyncStorage-backed); the user record is cached in AsyncStorage so the
 * shell can render immediately on cold start while `refresh()` revalidates.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';

import { SkyUser } from '../types';
import { apiFetch, tokenStorage } from './http';

const SESSION_KEY = 'skyai.session';

const cacheUser = (user: SkyUser | null): void => {
  if (user) {
    void AsyncStorage.setItem(SESSION_KEY, JSON.stringify(user)).catch(() => {});
  } else {
    void AsyncStorage.removeItem(SESSION_KEY).catch(() => {});
  }
};

export interface LoginPayload {
  email: string;
  password: string;
}

interface AuthResponse {
  user: SkyUser;
  token: string;
  expires_at: number;
}

export const auth = {
  /** Read the cached user from a previous session (async on RN). */
  async cached(): Promise<SkyUser | null> {
    try {
      const raw = await AsyncStorage.getItem(SESSION_KEY);
      return raw ? (JSON.parse(raw) as SkyUser) : null;
    } catch {
      return null;
    }
  },

  /** Re-fetch the authoritative user record from the backend. */
  async refresh(): Promise<SkyUser | null> {
    const token = tokenStorage.get();
    if (!token) {
      return null;
    }
    try {
      const data = await apiFetch<{ user: SkyUser }>('/api/v1/auth/me');
      cacheUser(data.user);
      return data.user;
    } catch {
      tokenStorage.set(null);
      cacheUser(null);
      return null;
    }
  },

  async login(payload: LoginPayload): Promise<SkyUser> {
    const data = await apiFetch<AuthResponse>('/api/v1/auth/login', {
      method: 'POST',
      body: { email: payload.email, password: payload.password },
      noAuth: true,
    });
    tokenStorage.set(data.token);
    cacheUser(data.user);
    return data.user;
  },

  async logout(): Promise<void> {
    try {
      await apiFetch('/api/v1/auth/logout', { method: 'POST' });
    } catch {
      /* ignore — clear local state regardless */
    }
    tokenStorage.set(null);
    cacheUser(null);
  },

  async changePassword(
    currentPassword: string,
    newPassword: string,
  ): Promise<void> {
    if (newPassword.length < 8) {
      throw new Error('New password must be at least 8 characters.');
    }
    await apiFetch('/api/v1/auth/change-password', {
      method: 'POST',
      body: { currentPassword, newPassword },
    });
  },

  async updateProfile(
    patch: Partial<Pick<SkyUser, 'fullName' | 'phone' | 'organization'>>,
  ): Promise<SkyUser> {
    const data = await apiFetch<{ user: SkyUser }>('/api/v1/auth/profile', {
      method: 'PATCH',
      body: patch,
    });
    cacheUser(data.user);
    return data.user;
  },
};
