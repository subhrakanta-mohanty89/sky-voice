import React, { useEffect, useState } from 'react';
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { PrimaryButton } from '../components/ui';
import { useVoice } from '../context/VoiceContext';
import { getSimCards } from '../services/api';
import { ensureMicPermission } from '../services/permissions';
import { SimCard } from '../types';
import { colors, radius, spacing } from '../theme';

const KEYS = [
  ['1', '2', '3'],
  ['4', '5', '6'],
  ['7', '8', '9'],
  ['*', '0', '#'],
];

export default function DialerScreen() {
  const { dial, phase } = useVoice();
  const [number, setNumber] = useState('');
  const [sims, setSims] = useState<SimCard[]>([]);
  const [fromNumber, setFromNumber] = useState<string | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getSimCards().then(list => {
      setSims(list);
      const def = list.find(s => s.isDefault) ?? list[0];
      if (def) {
        setFromNumber(def.phoneNumber);
      }
    });
  }, []);

  const press = (k: string) => setNumber(prev => (prev + k).slice(0, 20));
  const backspace = () => setNumber(prev => prev.slice(0, -1));

  const onCall = async () => {
    setError(null);
    const to = number.trim();
    if (!to) {
      setError('Enter a number to call.');
      return;
    }
    const granted = await ensureMicPermission();
    if (!granted) {
      setError('Microphone permission is required to place calls.');
      return;
    }
    await dial(to, fromNumber);
  };

  return (
    <SafeAreaView style={styles.flex} edges={['top']}>
      <ScrollView contentContainerStyle={styles.container}>
        <Text style={styles.display} numberOfLines={1} adjustsFontSizeToFit>
          {number || 'Enter number'}
        </Text>

        {sims.length > 0 ? (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            style={styles.simRow}
            contentContainerStyle={styles.simRowContent}>
            {sims.map(sim => {
              const active = sim.phoneNumber === fromNumber;
              return (
                <Pressable
                  key={sim.id}
                  onPress={() => setFromNumber(sim.phoneNumber)}
                  style={[styles.simChip, active && styles.simChipActive]}>
                  <Text
                    style={[
                      styles.simChipText,
                      active && styles.simChipTextActive,
                    ]}>
                    {sim.label || sim.phoneNumber}
                  </Text>
                </Pressable>
              );
            })}
          </ScrollView>
        ) : null}

        <View style={styles.pad}>
          {KEYS.map(row => (
            <View key={row.join()} style={styles.padRow}>
              {row.map(k => (
                <Pressable
                  key={k}
                  onPress={() => press(k)}
                  style={({ pressed }) => [
                    styles.key,
                    pressed && styles.keyPressed,
                  ]}>
                  <Text style={styles.keyText}>{k}</Text>
                </Pressable>
              ))}
            </View>
          ))}
        </View>

        {error ? <Text style={styles.error}>{error}</Text> : null}

        <View style={styles.actions}>
          <PrimaryButton
            label="Call"
            variant="success"
            onPress={onCall}
            loading={phase === 'connecting'}
            style={styles.callBtn}
          />
          <Pressable onPress={backspace} style={styles.backspace}>
            <Text style={styles.backspaceText}>⌫</Text>
          </Pressable>
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  container: { padding: spacing.lg, flexGrow: 1, justifyContent: 'center' },
  display: {
    color: colors.text,
    fontSize: 40,
    fontWeight: '700',
    textAlign: 'center',
    marginBottom: spacing.lg,
    minHeight: 56,
  },
  simRow: { marginBottom: spacing.lg, flexGrow: 0 },
  simRowContent: { gap: spacing.sm },
  simChip: {
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    borderRadius: radius.pill,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderColor: colors.border,
  },
  simChipActive: { backgroundColor: colors.primary, borderColor: colors.primary },
  simChipText: { color: colors.textMuted, fontWeight: '600' },
  simChipTextActive: { color: colors.white },
  pad: { gap: spacing.md },
  padRow: { flexDirection: 'row', justifyContent: 'space-between' },
  key: {
    width: 76,
    height: 76,
    borderRadius: 38,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
  },
  keyPressed: { backgroundColor: colors.surfaceAlt },
  keyText: { color: colors.text, fontSize: 30, fontWeight: '600' },
  actions: {
    flexDirection: 'row',
    alignItems: 'center',
    marginTop: spacing.xl,
    gap: spacing.md,
  },
  callBtn: { flex: 1 },
  backspace: {
    width: 52,
    height: 52,
    alignItems: 'center',
    justifyContent: 'center',
  },
  backspaceText: { color: colors.textMuted, fontSize: 24 },
  error: { color: colors.danger, textAlign: 'center', marginTop: spacing.md },
});
