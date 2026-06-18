/**
 * Design tokens — a slimmed-down version of the web app's slate/indigo theme
 * (the desktop + web shells use #0f172a as the base canvas).
 */

export const colors = {
  bg: '#0f172a', // slate-900 — app canvas
  surface: '#1e293b', // slate-800 — cards
  surfaceAlt: '#334155', // slate-700 — inputs / pressed
  border: '#334155',
  text: '#f1f5f9', // slate-100
  textMuted: '#94a3b8', // slate-400
  primary: '#6366f1', // indigo-500
  primaryDark: '#4f46e5', // indigo-600
  success: '#22c55e', // green-500
  danger: '#ef4444', // red-500
  warn: '#f59e0b', // amber-500
  white: '#ffffff',
};

export const spacing = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
  xl: 32,
};

export const radius = {
  sm: 8,
  md: 12,
  lg: 16,
  pill: 999,
};
