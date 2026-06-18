/**
 * VoiceService — a thin singleton wrapper around the native Twilio Voice
 * React Native SDK (`@twilio/voice-react-native-sdk`).
 *
 * This is the mobile equivalent of the web app's `useTwilioDevice` hook. The
 * key difference is that the native SDK talks to the platform's audio +
 * telephony stack directly (no WebRTC-in-a-WebView), so it works in a real
 * React Native runtime where `@twilio/voice-sdk` (browser-only) cannot.
 *
 * Token flow is identical to web/desktop: POST /api/v1/token returns a Twilio
 * AccessToken (VoiceGrant) that both `connect()` (outbound) and `register()`
 * (inbound) consume.
 *
 * NOTE on incoming calls: `register()` binds this device for inbound calls,
 * but on Android the call actually arrives as an FCM push (CallInvite). That
 * requires an FCM project + a Twilio Push Credential whose SID is embedded in
 * the access token. Until that's configured server-side, outbound calling
 * works fully and `register()` is a harmless no-op-on-failure. See README.
 */

import {
  Call,
  CallInvite,
  Voice,
} from '@twilio/voice-react-native-sdk';

import { APP_DISPLAY_NAME } from '../config';
import { getVoiceAccessToken } from './api';

export type VoicePhase =
  | 'idle'
  | 'registering'
  | 'ready'
  | 'connecting'
  | 'ringing'
  | 'in-call'
  | 'error'
  | 'unavailable';

export interface ActiveCallInfo {
  peer: string;
  sid: string;
  direction: 'inbound' | 'outbound';
  startedAt: number;
}

export interface IncomingInvite {
  from: string;
  sid: string;
}

interface VoiceListeners {
  phase: (phase: VoicePhase, error?: string | null) => void;
  active: (info: ActiveCallInfo | null) => void;
  incoming: (invite: IncomingInvite | null) => void;
  muted: (muted: boolean) => void;
}

type ListenerSets = {
  [K in keyof VoiceListeners]: Set<VoiceListeners[K]>;
};

class VoiceService {
  private voice: Voice | null = null;
  private activeCall: Call | null = null;
  private pendingInvite: CallInvite | null = null;
  private identity: string | null = null;

  private listeners: ListenerSets = {
    phase: new Set(),
    active: new Set(),
    incoming: new Set(),
    muted: new Set(),
  };

  // -- subscription -------------------------------------------------------
  on<K extends keyof VoiceListeners>(
    event: K,
    cb: VoiceListeners[K],
  ): () => void {
    this.listeners[event].add(cb as any);
    return () => this.listeners[event].delete(cb as any);
  }

  private emitPhase(phase: VoicePhase, error: string | null = null): void {
    this.listeners.phase.forEach(cb => cb(phase, error));
  }
  private emitActive(info: ActiveCallInfo | null): void {
    this.listeners.active.forEach(cb => cb(info));
  }
  private emitIncoming(invite: IncomingInvite | null): void {
    this.listeners.incoming.forEach(cb => cb(invite));
  }
  private emitMuted(muted: boolean): void {
    this.listeners.muted.forEach(cb => cb(muted));
  }

  getIdentity(): string | null {
    return this.identity;
  }

  // -- lifecycle ----------------------------------------------------------
  /**
   * Create the Voice instance, wire up listeners, and register for inbound
   * calls. Safe to call once after login.
   */
  async init(): Promise<void> {
    if (this.voice) {
      return;
    }
    this.emitPhase('registering');
    try {
      const { token, identity } = await getVoiceAccessToken();
      this.identity = identity;

      const voice = new Voice();
      this.voice = voice;

      voice.on(Voice.Event.CallInvite, (invite: CallInvite) => {
        this.pendingInvite = invite;
        this.emitIncoming({
          from: invite.getFrom() ?? 'Unknown',
          sid: invite.getCallSid() ?? '',
        });
      });
      voice.on(Voice.Event.Registered, () => this.emitPhase('ready'));
      voice.on(Voice.Event.Unregistered, () => this.emitPhase('unavailable'));
      voice.on(Voice.Event.Error, (err: { message?: string }) => {
        console.warn('Twilio Voice error:', err);
        this.emitPhase('error', err?.message ?? 'Voice error');
      });

      // Registering for inbound push can fail without an FCM/push credential
      // setup — that must not prevent outbound calling from working.
      try {
        await voice.register(token);
        this.emitPhase('ready');
      } catch (err) {
        console.warn(
          'voice.register failed (incoming push not configured?):',
          err,
        );
        this.emitPhase('ready');
      }
    } catch (err: any) {
      console.error('VoiceService.init failed:', err);
      this.emitPhase('error', err?.message ?? String(err));
    }
  }

  // -- outbound -----------------------------------------------------------
  async connect(to: string, from?: string): Promise<void> {
    if (!this.voice) {
      await this.init();
    }
    if (!this.voice) {
      this.emitPhase('error', 'Voice not initialised');
      return;
    }
    this.emitPhase('connecting');
    try {
      // Fresh token per call so an expired registration token never blocks a
      // dial. `FromNumber` is a custom TwiML param the backend
      // /twilio/voice/outgoing webhook reads to pick the caller-ID SIM
      // (Twilio reserves `From` for the SDK identity).
      const { token } = await getVoiceAccessToken();
      const params: Record<string, string> = { To: to };
      if (from) {
        params.FromNumber = from;
      }
      const call = await this.voice.connect(token, {
        params,
        notificationDisplayName: APP_DISPLAY_NAME,
      });
      this.activeCall = call;
      this.attachCallListeners(call, { peer: to, direction: 'outbound' });
      this.emitActive({
        peer: to,
        sid: call.getSid() ?? '',
        direction: 'outbound',
        startedAt: Date.now(),
      });
      this.emitPhase('in-call');
    } catch (err: any) {
      console.warn('Outbound dial failed:', err);
      this.emitPhase('error', err?.message ?? String(err));
    }
  }

  // -- inbound ------------------------------------------------------------
  async accept(): Promise<void> {
    const invite = this.pendingInvite;
    if (!invite) {
      return;
    }
    try {
      const call = await invite.accept();
      this.activeCall = call;
      this.pendingInvite = null;
      this.emitIncoming(null);
      this.attachCallListeners(call, {
        peer: invite.getFrom() ?? 'Unknown',
        direction: 'inbound',
      });
      this.emitActive({
        peer: invite.getFrom() ?? 'Unknown',
        sid: invite.getCallSid() ?? '',
        direction: 'inbound',
        startedAt: Date.now(),
      });
      this.emitPhase('in-call');
    } catch (err: any) {
      console.warn('Accept failed:', err);
      this.emitPhase('error', err?.message ?? String(err));
    }
  }

  async reject(): Promise<void> {
    const invite = this.pendingInvite;
    this.pendingInvite = null;
    this.emitIncoming(null);
    if (invite) {
      try {
        await invite.reject();
      } catch (err) {
        console.warn('Reject failed:', err);
      }
    }
    this.emitPhase('ready');
  }

  // -- in-call controls ---------------------------------------------------
  async hangup(): Promise<void> {
    const call = this.activeCall;
    if (call) {
      try {
        await call.disconnect();
      } catch (err) {
        console.warn('Hangup failed:', err);
      }
    }
    this.activeCall = null;
    this.emitActive(null);
    this.emitMuted(false);
    this.emitPhase('ready');
  }

  async toggleMute(): Promise<boolean> {
    const call = this.activeCall;
    if (!call) {
      return false;
    }
    try {
      const isMuted = call.isMuted?.() ?? false;
      const next = await call.mute(!isMuted);
      this.emitMuted(next);
      return next;
    } catch (err) {
      console.warn('Mute toggle failed:', err);
      return false;
    }
  }

  // -- internals ----------------------------------------------------------
  private attachCallListeners(
    call: Call,
    meta: { peer: string; direction: 'inbound' | 'outbound' },
  ): void {
    call.on(Call.Event.Connected, () => {
      this.emitActive({
        peer: meta.peer,
        sid: call.getSid() ?? '',
        direction: meta.direction,
        startedAt: Date.now(),
      });
      this.emitPhase('in-call');
    });
    call.on(Call.Event.Ringing, () => this.emitPhase('ringing'));
    call.on(Call.Event.Reconnecting, () => this.emitPhase('in-call'));
    call.on(Call.Event.Disconnected, (err?: { message?: string }) => {
      if (err) {
        console.warn('Call disconnected with error:', err);
      }
      this.activeCall = null;
      this.emitActive(null);
      this.emitMuted(false);
      this.emitPhase('ready');
    });
  }

  async teardown(): Promise<void> {
    try {
      await this.activeCall?.disconnect();
    } catch {
      /* ignore */
    }
    this.activeCall = null;
    this.pendingInvite = null;
    this.voice = null;
    this.identity = null;
    this.emitActive(null);
    this.emitIncoming(null);
    this.emitPhase('idle');
  }
}

export const voiceService = new VoiceService();
