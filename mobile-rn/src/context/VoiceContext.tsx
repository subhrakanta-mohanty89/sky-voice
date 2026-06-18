/**
 * VoiceContext — React bindings over the `voiceService` singleton.
 *
 * Exposes the current softphone phase, the active call, any pending incoming
 * invite, and the imperative call controls. Mirrors the shape of the web
 * app's `useTwilioDevice` return value so screens read familiarly.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

import {
  ActiveCallInfo,
  IncomingInvite,
  VoicePhase,
  voiceService,
} from '../services/voice';
import { useAuth } from './AuthContext';

interface VoiceContextValue {
  phase: VoicePhase;
  error: string | null;
  activeCall: ActiveCallInfo | null;
  incoming: IncomingInvite | null;
  isMuted: boolean;
  identity: string | null;
  dial: (to: string, from?: string) => Promise<void>;
  hangup: () => Promise<void>;
  accept: () => Promise<void>;
  reject: () => Promise<void>;
  toggleMute: () => Promise<void>;
}

const VoiceContext = createContext<VoiceContextValue | undefined>(undefined);

export function VoiceProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  const [phase, setPhase] = useState<VoicePhase>('idle');
  const [error, setError] = useState<string | null>(null);
  const [activeCall, setActiveCall] = useState<ActiveCallInfo | null>(null);
  const [incoming, setIncoming] = useState<IncomingInvite | null>(null);
  const [isMuted, setIsMuted] = useState(false);

  useEffect(() => {
    const unsubs = [
      voiceService.on('phase', (p, e) => {
        setPhase(p);
        setError(e ?? null);
      }),
      voiceService.on('active', setActiveCall),
      voiceService.on('incoming', setIncoming),
      voiceService.on('muted', setIsMuted),
    ];
    return () => unsubs.forEach(u => u());
  }, []);

  // Register the softphone once authenticated; tear down on logout.
  useEffect(() => {
    if (isAuthenticated) {
      void voiceService.init();
    } else {
      void voiceService.teardown();
    }
  }, [isAuthenticated]);

  const dial = useCallback(
    (to: string, from?: string) => voiceService.connect(to, from),
    [],
  );
  const hangup = useCallback(() => voiceService.hangup(), []);
  const accept = useCallback(() => voiceService.accept(), []);
  const reject = useCallback(() => voiceService.reject(), []);
  const toggleMute = useCallback(async () => {
    await voiceService.toggleMute();
  }, []);

  const value = useMemo<VoiceContextValue>(
    () => ({
      phase,
      error,
      activeCall,
      incoming,
      isMuted,
      identity: voiceService.getIdentity(),
      dial,
      hangup,
      accept,
      reject,
      toggleMute,
    }),
    [
      phase,
      error,
      activeCall,
      incoming,
      isMuted,
      dial,
      hangup,
      accept,
      reject,
      toggleMute,
    ],
  );

  return (
    <VoiceContext.Provider value={value}>{children}</VoiceContext.Provider>
  );
}

export function useVoice(): VoiceContextValue {
  const ctx = useContext(VoiceContext);
  if (!ctx) {
    throw new Error('useVoice must be used within a VoiceProvider');
  }
  return ctx;
}
