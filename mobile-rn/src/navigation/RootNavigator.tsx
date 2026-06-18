import React from 'react';
import { StyleSheet, Text, View } from 'react-native';
import {
  DarkTheme,
  NavigationContainer,
  Theme,
} from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';

import CallOverlay from '../screens/CallOverlay';
import CallHistoryScreen from '../screens/CallHistoryScreen';
import ContactsScreen from '../screens/ContactsScreen';
import DashboardScreen from '../screens/DashboardScreen';
import DialerScreen from '../screens/DialerScreen';
import LoginScreen from '../screens/LoginScreen';
import SettingsScreen from '../screens/SettingsScreen';
import { useAuth } from '../context/AuthContext';
import { colors } from '../theme';

const Tab = createBottomTabNavigator();

const navTheme: Theme = {
  ...DarkTheme,
  colors: {
    ...DarkTheme.colors,
    background: colors.bg,
    card: colors.surface,
    border: colors.border,
    primary: colors.primary,
    text: colors.text,
  },
};

const TAB_GLYPH: Record<string, string> = {
  Dashboard: '◎',
  Dialer: '⌗',
  History: '↻',
  Contacts: '☰',
  Settings: '⚙',
};

function TabIcon({ name, color }: { name: string; color: string }) {
  return <Text style={[styles.tabGlyph, { color }]}>{TAB_GLYPH[name] ?? '•'}</Text>;
}

function MainTabs() {
  return (
    <Tab.Navigator
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarActiveTintColor: colors.primary,
        tabBarInactiveTintColor: colors.textMuted,
        tabBarStyle: styles.tabBar,
        tabBarIcon: ({ color }) => <TabIcon name={route.name} color={color} />,
      })}>
      <Tab.Screen name="Dashboard" component={DashboardScreen} />
      <Tab.Screen name="Dialer" component={DialerScreen} />
      <Tab.Screen name="History" component={CallHistoryScreen} />
      <Tab.Screen name="Contacts" component={ContactsScreen} />
      <Tab.Screen name="Settings" component={SettingsScreen} />
    </Tab.Navigator>
  );
}

export default function RootNavigator() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <View style={styles.splash}>
        <Text style={styles.splashText}>Sky Voice AI</Text>
      </View>
    );
  }

  return (
    <NavigationContainer theme={navTheme}>
      {isAuthenticated ? <MainTabs /> : <LoginScreen />}
      {/* Global call UI sits above whichever shell is mounted. */}
      <CallOverlay />
    </NavigationContainer>
  );
}

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    backgroundColor: colors.bg,
    alignItems: 'center',
    justifyContent: 'center',
  },
  splashText: { color: colors.text, fontSize: 24, fontWeight: '800' },
  tabBar: {
    backgroundColor: colors.surface,
    borderTopColor: colors.border,
    height: 60,
    paddingBottom: 8,
    paddingTop: 6,
  },
  tabGlyph: { fontSize: 20 },
});
