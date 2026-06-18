/**
 * App configuration.
 *
 * The backend is the same Cloud Run service the web frontend and the desktop
 * app talk to. React Native has no `import.meta.env`, so the base URLs are
 * plain constants here. Point `API_BASE_URL` at a local backend
 * (e.g. http://10.0.2.2:8000 from the Android emulator) during development.
 */

export const API_BASE_URL =
  'https://mi-sky-ai-backend-1065257134948.us-central1.run.app';

export const WS_BASE_URL =
  'wss://mi-sky-ai-backend-1065257134948.us-central1.run.app';

/** Display name shown in the Twilio call notification / CallKit handle. */
export const APP_DISPLAY_NAME = 'Sky Voice AI';
