import React from 'react';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Card, PrimaryButton } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { useVoice } from '../context/VoiceContext';
import { API_BASE_URL } from '../config';
import { colors, spacing } from '../theme';

function Row({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.rowLabel}>{label}</Text>
      <Text style={styles.rowValue} numberOfLines={1}>
        {value}
      </Text>
    </View>
  );
}

export default function SettingsScreen() {
  const { user, logout } = useAuth();
  const { identity, phase } = useVoice();

  return (
    <SafeAreaView style={styles.flex} edges={['top']}>
      <ScrollView contentContainerStyle={styles.container}>
        <Text style={styles.header}>Settings</Text>

        <Card style={styles.card}>
          <Text style={styles.cardTitle}>Account</Text>
          <Row label="Name" value={user?.fullName ?? '—'} />
          <Row label="Email" value={user?.email ?? '—'} />
          <Row label="Role" value={user?.role ?? '—'} />
          {user?.organization ? (
            <Row label="Organization" value={user.organization} />
          ) : null}
        </Card>

        <Card style={styles.card}>
          <Text style={styles.cardTitle}>Softphone</Text>
          <Row label="Identity" value={identity ?? '—'} />
          <Row label="Status" value={phase} />
        </Card>

        <Card style={styles.card}>
          <Text style={styles.cardTitle}>Backend</Text>
          <Row label="API" value={API_BASE_URL.replace(/^https?:\/\//, '')} />
          <Row label="App" value="Sky Voice AI · React Native" />
        </Card>

        <PrimaryButton label="Sign out" variant="danger" onPress={logout} />
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  container: { padding: spacing.lg },
  header: {
    color: colors.text,
    fontSize: 26,
    fontWeight: '800',
    marginBottom: spacing.lg,
  },
  card: { marginBottom: spacing.md },
  cardTitle: {
    color: colors.textMuted,
    fontSize: 13,
    fontWeight: '700',
    textTransform: 'uppercase',
    marginBottom: spacing.sm,
  },
  row: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: spacing.sm,
    gap: spacing.md,
  },
  rowLabel: { color: colors.textMuted, fontSize: 15 },
  rowValue: { color: colors.text, fontSize: 15, fontWeight: '600', flexShrink: 1 },
});
