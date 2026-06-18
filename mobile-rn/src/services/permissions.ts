/**
 * Runtime permission helpers. Android requires an explicit RECORD_AUDIO grant
 * at call time; iOS handles the mic prompt natively when audio starts.
 */

import { PermissionsAndroid, Platform } from 'react-native';

export async function ensureMicPermission(): Promise<boolean> {
  if (Platform.OS !== 'android') {
    return true;
  }
  try {
    const result = await PermissionsAndroid.request(
      PermissionsAndroid.PERMISSIONS.RECORD_AUDIO,
      {
        title: 'Microphone access',
        message: 'Sky Voice AI needs your microphone to place and receive calls.',
        buttonPositive: 'Allow',
        buttonNegative: 'Deny',
      },
    );
    return result === PermissionsAndroid.RESULTS.GRANTED;
  } catch (err) {
    console.warn('Mic permission request failed:', err);
    return false;
  }
}
