import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  FlatList,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { Field, PrimaryButton } from '../components/ui';
import { useVoice } from '../context/VoiceContext';
import { contactsService } from '../services/contacts';
import { ensureMicPermission } from '../services/permissions';
import { Contact } from '../types';
import { colors, radius, spacing } from '../theme';

function initials(name: string): string {
  return name
    .split(' ')
    .filter(Boolean)
    .slice(0, 2)
    .map(p => p[0]?.toUpperCase() ?? '')
    .join('');
}

export default function ContactsScreen() {
  const { dial } = useVoice();
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');

  const load = useCallback(async () => {
    setContacts(await contactsService.list());
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onAdd = async () => {
    if (!name.trim() || !phone.trim()) {
      return;
    }
    await contactsService.add({ fullName: name, phoneNumber: phone });
    setName('');
    setPhone('');
    setShowAdd(false);
    load();
  };

  const onCall = async (c: Contact) => {
    const granted = await ensureMicPermission();
    if (!granted) {
      Alert.alert('Microphone needed', 'Allow microphone access to place calls.');
      return;
    }
    await dial(c.phoneNumber);
  };

  const onLongPress = (c: Contact) => {
    Alert.alert(c.fullName, c.phoneNumber, [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Delete',
        style: 'destructive',
        onPress: async () => {
          await contactsService.remove(c.id);
          load();
        },
      },
    ]);
  };

  return (
    <SafeAreaView style={styles.flex} edges={['top']}>
      <View style={styles.headerRow}>
        <Text style={styles.header}>Contacts</Text>
        <Pressable onPress={() => setShowAdd(true)} style={styles.addBtn}>
          <Text style={styles.addBtnText}>＋</Text>
        </Pressable>
      </View>

      <FlatList
        data={contacts}
        keyExtractor={c => c.id}
        contentContainerStyle={
          contacts.length === 0 ? styles.emptyWrap : styles.listContent
        }
        ListEmptyComponent={
          <Text style={styles.empty}>No contacts yet. Tap ＋ to add one.</Text>
        }
        renderItem={({ item }) => (
          <Pressable
            style={styles.row}
            onPress={() => onCall(item)}
            onLongPress={() => onLongPress(item)}>
            <View style={styles.avatar}>
              <Text style={styles.avatarText}>{initials(item.fullName)}</Text>
            </View>
            <View style={styles.rowBody}>
              <Text style={styles.name}>{item.fullName}</Text>
              <Text style={styles.phone}>{item.phoneNumber}</Text>
            </View>
            <Text style={styles.callHint}>Call</Text>
          </Pressable>
        )}
      />

      <Modal visible={showAdd} animationType="slide" transparent>
        <View style={styles.modalBackdrop}>
          <View style={styles.modalCard}>
            <Text style={styles.modalTitle}>New contact</Text>
            <Field label="Name" value={name} onChangeText={setName} placeholder="Jane Doe" />
            <Field
              label="Phone"
              value={phone}
              onChangeText={setPhone}
              keyboardType="phone-pad"
              placeholder="+1 555 010 1234"
            />
            <View style={styles.modalActions}>
              <PrimaryButton
                label="Cancel"
                variant="ghost"
                onPress={() => setShowAdd(false)}
                style={styles.modalBtn}
              />
              <PrimaryButton label="Save" onPress={onAdd} style={styles.modalBtn} />
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: colors.bg },
  headerRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: spacing.lg,
    paddingBottom: spacing.md,
  },
  header: { color: colors.text, fontSize: 26, fontWeight: '800' },
  addBtn: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: colors.primary,
    alignItems: 'center',
    justifyContent: 'center',
  },
  addBtnText: { color: colors.white, fontSize: 24, fontWeight: '700', lineHeight: 26 },
  listContent: { paddingHorizontal: spacing.lg, paddingBottom: spacing.xl },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: colors.border,
    gap: spacing.md,
  },
  avatar: {
    width: 44,
    height: 44,
    borderRadius: 22,
    backgroundColor: colors.surfaceAlt,
    alignItems: 'center',
    justifyContent: 'center',
  },
  avatarText: { color: colors.text, fontWeight: '700' },
  rowBody: { flex: 1 },
  name: { color: colors.text, fontSize: 16, fontWeight: '600' },
  phone: { color: colors.textMuted, fontSize: 13, marginTop: 2 },
  callHint: { color: colors.success, fontWeight: '700' },
  emptyWrap: { flexGrow: 1, alignItems: 'center', justifyContent: 'center', padding: spacing.lg },
  empty: { color: colors.textMuted, fontSize: 15, textAlign: 'center' },
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.6)',
    justifyContent: 'flex-end',
  },
  modalCard: {
    backgroundColor: colors.surface,
    borderTopLeftRadius: radius.lg,
    borderTopRightRadius: radius.lg,
    padding: spacing.lg,
  },
  modalTitle: {
    color: colors.text,
    fontSize: 20,
    fontWeight: '800',
    marginBottom: spacing.md,
  },
  modalActions: { flexDirection: 'row', gap: spacing.md, marginTop: spacing.sm },
  modalBtn: { flex: 1 },
});
