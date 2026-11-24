import React, { useState } from "react";
import { Alert, ScrollView, StyleSheet, Text } from "react-native";
import { getApiBaseUrl, setApiBaseUrl } from "../api/client";
import { AUTH_NOT_CONFIGURED } from "../api/auth";
import { colors } from "../theme";
import { ActionButton, Field, PageTitle, Panel } from "../components/common";

export default function SettingsScreen() {
  const [url, setUrl] = useState(getApiBaseUrl());
  const save = () => {
    try { setApiBaseUrl(url); Alert.alert("Backend URL updated", "New requests will use the configured CodeDeck server."); }
    catch (error) { Alert.alert("Invalid backend URL", error instanceof Error ? error.message : "Enter a valid URL."); }
  };
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Settings" subtitle="Connect this manager app to the shared CodeDeck API." />
    <Panel style={styles.panel}><Text style={styles.heading}>Backend connection</Text><Text style={styles.body}>Use a server address reachable from this device. This value is kept in memory for the current app session; configure EXPO_PUBLIC_API_BASE_URL for a fixed development endpoint.</Text><Field value={url} onChangeText={setUrl} placeholder="https://codedeck.example.com" /><ActionButton title="Use this backend" onPress={save} /></Panel>
    <Panel style={styles.auth}><Text style={styles.authTitle}>Authentication status · Not configured</Text><Text style={styles.body}>{AUTH_NOT_CONFIGURED}</Text><Text style={styles.body}>The current backend accepts a shared release-approval service token and caller-supplied identity labels. Neither verifies a manager account. The app deliberately does not request, save or package that backend secret.</Text></Panel>
    <Panel style={styles.panel}><Text style={styles.heading}>Notifications</Text><Text style={styles.body}>In-app activity is available from persisted monitoring events. Push notifications require notification infrastructure and are not enabled.</Text></Panel>
    <Text style={styles.version}>CodeDeck Manager · 0.1.0</Text>
  </ScrollView>;
}

const styles = StyleSheet.create({ content: { padding: 18, paddingBottom: 40, gap: 12 }, panel: { gap: 12 }, heading: { color: colors.ink, fontSize: 15, fontWeight: "700" }, body: { color: colors.secondary, fontSize: 13, lineHeight: 20 }, auth: { gap: 8, backgroundColor: colors.amberSoft, borderColor: "#F1D99A" }, authTitle: { color: colors.amber, fontSize: 14, fontWeight: "700" }, version: { textAlign: "center", color: colors.muted, fontSize: 11, marginTop: 12 }, });
