/**
 * REST client for the Sky Voice AI Twilio backend (React Native).
 *
 * A focused subset of the web app's `services/api.ts`: the endpoints the
 * mobile softphone actually uses. All endpoints expect a Bearer JWT, which
 * `apiFetch` attaches automatically from `tokenStorage`.
 */

import { apiFetch } from './http';
import { CallHistoryEntry, MakeCallResponse, SimCard } from '../types';

// ---------------------------------------------------------------------------
// Twilio Voice access token (used by the native Twilio Voice SDK)
// ---------------------------------------------------------------------------

export interface VoiceTokenResponse {
  success: boolean;
  token: string;
  identity: string;
  expires_in: number;
}

export async function getVoiceAccessToken(): Promise<VoiceTokenResponse> {
  return apiFetch<VoiceTokenResponse>('/api/v1/token', {
    method: 'POST',
    body: {},
  });
}

// ---------------------------------------------------------------------------
// Outbound calls (REST fallback / call control)
// ---------------------------------------------------------------------------

export async function makeCall(
  to: string,
  from?: string,
): Promise<MakeCallResponse> {
  const body: { to: string; from_number?: string } = { to };
  if (from) {
    body.from_number = from;
  }
  return apiFetch<MakeCallResponse>('/api/make-call', {
    method: 'POST',
    body,
  });
}

export async function endCall(
  callUuid: string,
): Promise<{ success: boolean; error?: string }> {
  try {
    return await apiFetch(`/api/end-call/${callUuid}`, { method: 'POST' });
  } catch (err: any) {
    return { success: false, error: err?.message ?? String(err) };
  }
}

// ---------------------------------------------------------------------------
// Agent presence (Available / Away / Offline)
// ---------------------------------------------------------------------------

export type AgentPresence = 'available' | 'away' | 'offline';

export interface AgentPresenceResponse {
  success: boolean;
  agent?: {
    id: string;
    identity: string;
    name: string;
    role: string;
    status: 'available' | 'busy' | 'away' | 'offline';
    presence: AgentPresence;
    current_call_uuid: string | null;
  } | null;
  error?: string;
}

export async function setMyPresence(
  presence: AgentPresence,
): Promise<AgentPresenceResponse> {
  return apiFetch<AgentPresenceResponse>('/api/v1/agents/me/presence', {
    method: 'POST',
    body: { presence },
  });
}

// ---------------------------------------------------------------------------
// Call history
// ---------------------------------------------------------------------------

export interface CallHistoryResponse {
  call_history: CallHistoryEntry[];
  total: number;
}

export async function getCallHistory(
  limit: number = 50,
): Promise<CallHistoryResponse> {
  try {
    return await apiFetch<CallHistoryResponse>(
      `/api/call-history?limit=${limit}`,
    );
  } catch (err) {
    console.warn('getCallHistory failed, returning empty list:', err);
    return { call_history: [], total: 0 };
  }
}

// ---------------------------------------------------------------------------
// SIM cards (outbound caller IDs)
// ---------------------------------------------------------------------------

export async function getSimCards(): Promise<SimCard[]> {
  try {
    const data = await apiFetch<{ sims: SimCard[] }>('/api/v1/sims');
    return data.sims ?? [];
  } catch (err) {
    console.warn('getSimCards failed, returning empty list:', err);
    return [];
  }
}
