/**
 * Contacts service (React Native).
 *
 * The web app keeps contacts client-side in localStorage (there is no backend
 * contacts endpoint yet), so the mobile app mirrors that with an
 * AsyncStorage-backed store.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';

import { Contact } from '../types';

const KEY = 'skyai.contacts';

async function readAll(): Promise<Contact[]> {
  try {
    const raw = await AsyncStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Contact[]) : [];
  } catch {
    return [];
  }
}

async function writeAll(contacts: Contact[]): Promise<void> {
  try {
    await AsyncStorage.setItem(KEY, JSON.stringify(contacts));
  } catch {
    /* ignore write failures */
  }
}

export const contactsService = {
  list(): Promise<Contact[]> {
    return readAll();
  },

  async add(input: {
    fullName: string;
    phoneNumber: string;
    organization?: string;
  }): Promise<Contact> {
    const now = new Date().toISOString();
    const contact: Contact = {
      id: `c_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      fullName: input.fullName.trim(),
      phoneNumber: input.phoneNumber.trim(),
      organization: input.organization?.trim() || undefined,
      isFavorite: false,
      createdAt: now,
      updatedAt: now,
    };
    const all = await readAll();
    all.unshift(contact);
    await writeAll(all);
    return contact;
  },

  async remove(id: string): Promise<void> {
    const all = await readAll();
    await writeAll(all.filter(c => c.id !== id));
  },
};
