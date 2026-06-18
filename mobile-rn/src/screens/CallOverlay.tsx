import React, { useEffect, useRef, useState } from 'react';
import {
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';

import { useVoice } from '../context/VoiceContext';
import { colors, radius, spacing } from '../theme';

function useCallTimer(active: boolean, startedAt?: number) {
  const [elapsed, setElapsed] = useState(0);
  const ref = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (active && startedAt) {
      ref.current = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startedAt) / 1000));
      }, 1000);
    } else {
      setElapsed(0);
    }
    return () => {
      if (ref.current) {
        clearInterval(ref.current);
      }
    };
  }, [active, startedAt]);
  const m = Math.floor(elapsed / 60);
  const s = elapsed % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

const PHASE_LABEL: Record<string, string> = {
  connecting: 'Calling…',
  ringing: 'Ringing…',
  'in-call': 'Connected',
};

export default function CallOverlay() {
  const {
    phase,
    activeCall,
    incoming,
    isMuted,
    hangup,
    accept,
    reject,
    toggleMute,
  } = useVoice();

  const inCallPhase =
    phase === 'connecting' || phase === 'ringing' || phase === 'in-call';
  const timer = useCallTimer(phase === 'in-call', activeCall?.startedAt);

  // Incoming invite takes priority.
  if (incoming && !activeCall) {
    return (
      <Modal visible animationType="fade" transparent={false}>
        <View style={styles.full}>
          <View style={styles.topBlock}>
            <Text style={styles.incomingLabel}>Incoming call</Text>
            <Text style={styles.peer}>{incoming.from}</Text>
          </View>
          <View style={styles.actionsRow}>
            <CircleButton color={colors.danger} label="Decline" glyph="✕" onPress={reject} />
            <CircleButton color={colors.success} label="Accept" glyph="✓" onPress={accept} />
          </View>
        </View>
      </Modal>
    );
  }

  if (!inCallPhase || !activeCall) {
    return null;
  }

  return (
    <Modal visible animationType="slide" transparent={false}>
      <View style={styles.full}>
        <View style={styles.topBlock}>
          <Text style={styles.statusLabel}>
            {PHASE_LABEL[phase] ?? 'On a call'}
          </Text>
          <Text style={styles.peer}>{activeCall.peer}</Text>
          {phase === 'in-call' ? (
            <Text style={styles.timer}>{timer}</Text>
          ) : null}
        </View>

        <View style={styles.controls}>
          <Pressable
            onPress={toggleMute}
            style={[styles.controlBtn, isMuted && styles.controlBtnActive]}>
            <Text style={styles.controlGlyph}>{isMuted ? '🔇' : '🎙'}</Text>
            <Text style={styles.controlLabel}>{isMuted ? 'Muted' : 'Mute'}</Text>
          </Pressable>
        </View>

        <View style={styles.actionsRow}>
          <CircleButton
            color={colors.danger}
            label="End"
            glyph="✕"
            onPress={hangup}
          />
        </View>
      </View>
    </Modal>
  );
}

function CircleButton({
  color,
  label,
  glyph,
  onPress,
}: {
  color: string;
  label: string;
  glyph: string;
  onPress: () => void;
}) {
  return (
    <View style={styles.circleWrap}>
      <Pressable
        onPress={onPress}
        style={({ pressed }) => [
          styles.circle,
          { backgroundColor: color, opacity: pressed ? 0.85 : 1 },
        ]}>
        <Text style={styles.circleGlyph}>{glyph}</Text>
      </Pressable>
      <Text style={styles.circleLabel}>{label}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  full: {
    flex: 1,
    backgroundColor: colors.bg,
    justifyContent: 'space-between',
    paddingVertical: spacing.xl * 2,
    paddingHorizontal: spacing.lg,
  },
  topBlock: { alignItems: 'center', marginTop: spacing.xl * 2 },
  incomingLabel: {
    color: colors.success,
    fontSize: 16,
    fontWeight: '700',
    marginBottom: spacing.md,
  },
  statusLabel: {
    color: colors.textMuted,
    fontSize: 16,
    marginBottom: spacing.md,
  },
  peer: { color: colors.text, fontSize: 32, fontWeight: '800', textAlign: 'center' },
  timer: { color: colors.textMuted, fontSize: 18, marginTop: spacing.md },
  controls: { alignItems: 'center' },
  controlBtn: {
    width: 84,
    height: 84,
    borderRadius: 42,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
  },
  controlBtnActive: { backgroundColor: colors.surfaceAlt },
  controlGlyph: { fontSize: 26 },
  controlLabel: { color: colors.textMuted, fontSize: 12 },
  actionsRow: {
    flexDirection: 'row',
    justifyContent: 'space-evenly',
    alignItems: 'center',
  },
  circleWrap: { alignItems: 'center', gap: spacing.sm },
  circle: {
    width: 76,
    height: 76,
    borderRadius: 38,
    alignItems: 'center',
    justifyContent: 'center',
  },
  circleGlyph: { color: colors.white, fontSize: 30, fontWeight: '800' },
  circleLabel: { color: colors.textMuted, fontSize: 14 },
});
