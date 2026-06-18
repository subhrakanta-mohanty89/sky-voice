import React, { useEffect, useState } from 'react';
import {
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Card, PrimaryButton } from '../components/ui';
import { useAuth } from '../context/AuthContext';
import { useVoice } from '../context/VoiceContext';
import {
  AgentPresence,
  getCallHistory,
  setMyPresence,
} from '../services/api';
import { CallHistoryEntry } from '../types';
import { colors, radius, spacing } from '../theme';

const PHASE_LABEL: Record<string, string> = {
  idle: 'Offline',
  registering: 'Connecting…',
  ready: 'Ready',
  connecting: 'Dialing…',
  ringing: 'Ringing…',
  'in-call': 'On a call',
  error: 'Error',
  unavailable: 'Unavailable',
};

export default function DashboardScreen() {
  const { user } = useAuth();
  const { phase } = useVoice();
  const [history, setHistory] = useState<CallHistoryEntry[]>([]);
  const [presence, setPresence] = useState<AgentPresence>('available');
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    const data = await getCallHistory(20);
    setHistory(data.call_history);
  };

  useEffect(() => {
    load();
  }, []);

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const changePresence = async (p: AgentPresence) => {
    setPresence(p);
    try {
      await setMyPresence(p);
    } catch {
      /* non-fatal */
    }
  };

  const today = new Date().toDateString();
  const callsToday = history.filter(
    h => new Date(h.timestamp).toDateString() === today,
  ).length;

  const ready = phase === 'ready' || phase === 'in-call';

  return (
    <SafeAreaView style={styles.flex} edges={['top']}>
      <ScrollView
        contentContainerStyle={styles.container}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={colors.textMuted} />
        }>
        <Text style={styles.greeting}>
          Hi, {user?.fullName?.split(' ')[0] ?? 'there'}
        </Text>

        <Card style={styles.statusCard}>
          <View style={styles.statusRow}>
            <View
              style={[
                styles.dot,
                { backgroundColor: ready ? colors.success : colors.warn },
              ]}
            />
            <Text style={styles.statusText}>
              Softphone: {PHASE_LABEL[phase] ?? phase}
            </Text>
          </View>
        </Card>

        <View style={styles.statsRow}>
          <Card style={styles.stat}>
            <Text style={styles.statNum}>{callsToday}</Text>
            <Text style={styles.statLabel}>Calls today</Text>
          </Card>
          <Card style={styles.stat}>
            <Text style={styles.statNum}>{history.length}</Text>
            <Text style={styles.statLabel}>Recent calls</Text>
          </Card>
        </View>

        <Text style={styles.section}>Presence</Text>
        <View style={styles.presenceRow}>
          {(['available', 'away', 'offline'] as AgentPresence[]).map(p => (
            <PrimaryButton
              key={p}
              label={p[0].toUpperCase() + p.slice(1)}
              variant={presence === p ? 'primary' : 'ghost'}
              onPress={() => changePresence(p)}
              style={styles.presenceBtn}
            />
          ))}
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  container: { padding: spacing.lg },
  greeting: {
    color: colors.text,
    fontSize: 28,
    fontWeight: '800',
    marginBottom: spacing.lg,
  },
  statusCard: { marginBottom: spacing.md },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  dot: { width: 12, height: 12, borderRadius: 6 },
  statusText: { color: colors.text, fontSize: 16, fontWeight: '600' },
  statsRow: { flexDirection: 'row', gap: spacing.md, marginBottom: spacing.lg },
  stat: { flex: 1, alignItems: 'center', paddingVertical: spacing.lg },
  statNum: { color: colors.text, fontSize: 32, fontWeight: '800' },
  statLabel: { color: colors.textMuted, fontSize: 13, marginTop: spacing.xs },
  section: {
    color: colors.textMuted,
    fontSize: 13,
    fontWeight: '700',
    textTransform: 'uppercase',
    marginBottom: spacing.sm,
  },
  presenceRow: { flexDirection: 'row', gap: spacing.sm },
  presenceBtn: { flex: 1, height: 44, paddingHorizontal: spacing.sm },
});
