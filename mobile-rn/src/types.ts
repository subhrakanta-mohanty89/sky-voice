/**
 * Shared domain types — copied verbatim from the web frontend
 * (frontend/src/types.ts). These are pure TypeScript interfaces with no DOM
 * dependency, so they are reused as-is across web, desktop and mobile.
 */

export interface Call {
  call_uuid: string;
  status: string;
  from: string;
  to?: string;
  type?: 'inbound' | 'outbound' | 'unknown';
  direction?: 'inbound' | 'outbound' | 'unknown';
  operator_answered?: boolean;
  operator_connected: boolean;
  is_on_hold?: boolean;
  waiting_for_operator?: boolean;
  language?: string;
  service_code?: string | null;
  service_label?: string | null;
  websocket_url: string;
}

export interface Message {
  id: string;
  type: 'customer' | 'operator' | 'system';
  text: string;
  translated?: string;
  originalHindi?: string;
  timestamp: Date;
}

export interface MakeCallResponse {
  success: boolean;
  call_uuid: string;
  to: string;
  from: string;
  websocket_url: string;
  message: string;
  error?: string;
}

export interface ActiveCallsResponse {
  active_calls: Call[];
}

// ---- Auth ------------------------------------------------------------------
export interface SkyUser {
  id: string;
  fullName: string;
  email: string;
  phone?: string;
  avatarInitials: string;
  organization?: string;
  role: 'admin' | 'member' | 'operator' | 'agent';
  status?: 'active' | 'inactive';
  emailVerified?: boolean;
  createdAt: string;
  updatedAt?: string;
  invitedBy?: string | null;
}

export interface AuthState {
  user: SkyUser | null;
  isAuthenticated: boolean;
  isLoading: boolean;
}

// ---- Contacts --------------------------------------------------------------
export interface Contact {
  id: string;
  fullName: string;
  phoneNumber: string;
  email?: string;
  organization?: string;
  notes?: string;
  isFavorite: boolean;
  tags?: string[];
  createdAt: string;
  updatedAt: string;
}

// ---- SIM cards -------------------------------------------------------------
export interface SimCard {
  id: string;
  phoneNumber: string;
  label: string;
  isDefault: boolean;
  source: 'twilio' | 'manual' | 'verified';
  twilioSid?: string | null;
  registeredAt: string;
}

// ---- Call history ----------------------------------------------------------
export interface CallHistoryEntry {
  call_uuid: string;
  type: string;
  direction: string;
  from: string;
  to: string;
  status: string;
  timestamp: string;
  ended_by: string;
  duration_seconds?: number | null;
  recording_url?: string | null;
}
