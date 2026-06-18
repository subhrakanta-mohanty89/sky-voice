/**
 * Sky Voice AI — React Native entry point.
 *
 * Mounts the provider stack (safe-area → auth → voice) and the root
 * navigator. Calling is handled by the native Twilio Voice SDK, so this runs
 * in a real RN runtime (unlike the browser-only @twilio/voice-sdk the web app
 * uses).
 *
 * @format
 */

import React from 'react';
import {StatusBar} from 'react-native';
import {SafeAreaProvider} from 'react-native-safe-area-context';

import {AuthProvider} from './src/context/AuthContext';
import {VoiceProvider} from './src/context/VoiceContext';
import RootNavigator from './src/navigation/RootNavigator';
import {colors} from './src/theme';

function App(): React.JSX.Element {
  return (
    <SafeAreaProvider>
      <StatusBar barStyle="light-content" backgroundColor={colors.bg} />
      <AuthProvider>
        <VoiceProvider>
          <RootNavigator />
        </VoiceProvider>
      </AuthProvider>
    </SafeAreaProvider>
  );
}

export default App;
