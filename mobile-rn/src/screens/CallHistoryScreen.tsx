import React, { useCallback, useEffect, useState } from 'react';
import { FlatList, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { getCallHistory } from '../services/api';
import { CallHistoryEntry } from '../types';
import { colors, radius, spacing } from '../theme';

function formatDuration(seconds?: number | null): string {
  if (!seconds || seconds <= 0) {
    return '—';
  }
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export default function CallHistoryScreen() {
  const [history, setHistory] = useState<CallHistoryEntry[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    const data = await getCallHistory(100);
    setHistory(data.call_history);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const renderItem = ({ item }: { item: CallHistoryEntry }) => {
    const inbound = item.direction === 'inbound';
    const peer = inbound ? item.from : item.to;
    return (
      <View style={styles.row}>
        <View
          style={[
            styles.iconCircle,
            { backgroundColor: inbound ? colors.surfaceAlt : colors.primaryDark },
          ]}>
          <Text style={styles.iconText}>{inbound ? '↓' : '↑'}</Text>
        </View>
        <View style={styles.rowBody}>
          <Text style={styles.peer} numberOfLines={1}>
            {peer || 'Unknown'}
          </Text>
          <Text style={styles.meta}>
            {inbound ? 'Incoming' : 'Outgoing'} · {item.status} ·{' '}
            {formatWhen(item.timestamp)}
          </Text>
        </View>
        <Text style={styles.duration}>
          {formatDuration(item.duration_seconds)}
        </Text>
      </View>
    );
  };

  return (
    <SafeAreaView style={styles.flex} edges={['top']}>
      <Text style={styles.header}>Call history</Text>
      <FlatList
        data={history}
        keyExtractor={(item, idx) => `${item.call_uuid}-${idx}`}
        renderItem={renderItem}
        onRefresh={load}
        refreshing={refreshing}
        contentContainerStyle={
          history.length === 0 ? styles.emptyWrap : styles.listContent
        }
        ListEmptyComponent={
          <Text style={styles.empty}>No calls yet.</Text>
        }
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  header: {
    color: colors.text,
    fontSize: 26,
    fontWeight: '800',
    padding: spacing.lg,
    paddingBottom: spacing.md,
  },
  listContent: { paddingHorizontal: spacing.lg, paddingBottom: spacing.xl },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: spacing.md,
  },
  iconCircle: {
    width: 40,
    height: 40,
    borderRadius: 20,
    alignItems: 'center',
    justifyContent: 'center',
  },
  iconText: { color: colors.white, fontSize: 18, fontWeight: '700' },
  rowBody: { flex: 1 },
  peer: { color: colors.text, fontSize: 16, fontWeight: '600' },
  meta: { color: colors.textMuted, fontSize: 13, marginTop: 2 },
  duration: { color: colors.textMuted, fontSize: 14 },
  emptyWrap: { flexGrow: 1, alignItems: 'center', justifyContent: 'center' },
  empty: { color: colors.textMuted, fontSize: 15 },
});
