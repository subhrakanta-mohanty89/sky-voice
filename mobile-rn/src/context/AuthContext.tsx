/**
 * AuthContext — holds the signed-in user and exposes login/logout.
 *
 * On cold start it hydrates the persisted JWT, paints the shell from the
 * cached user, then revalidates against /api/v1/auth/me in the background.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

import { auth } from '../services/auth';
import { tokenStorage } from '../services/http';
import { SkyUser } from '../types';

interface AuthContextValue {
  user: SkyUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  setUser: (user: SkyUser) => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUserState] = useState<SkyUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      await tokenStorage.hydrate();
      const cached = await auth.cached();
      if (!cancelled && cached) {
        setUserState(cached);
      }
      const fresh = await auth.refresh();
      if (!cancelled) {
        setUserState(fresh);
        setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    const u = await auth.login({ email, password });
    setUserState(u);
  }, []);

  const logout = useCallback(async () => {
    await auth.logout();
    setUserState(null);
  }, []);

  const setUser = useCallback((u: SkyUser) => setUserState(u), []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      isAuthenticated: !!user,
      isLoading,
      login,
      logout,
      setUser,
    }),
    [user, isLoading, login, logout, setUser],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return ctx;
}
