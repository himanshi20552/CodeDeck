import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { normalizeActivity } from "../api/activity.mjs";
import { activityViewState } from "../api/activityState.mjs";
import { colors } from "../theme";
import { formatDate, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

export default function NotificationsScreen() {
  const { data, loading, error, refresh } = useRemote(() => api.monitoring());
  const approvals = useRemote(api.approvalHistory);
  const entries = normalizeActivity(data, approvals.data);
  const state = activityViewState({ loading, error, entries });
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Activity" subtitle="Recent alerts, deployments and repository events reported by CodeDeck." />
    <Panel style={styles.note}><Text style={styles.noteTitle}>In-app activity feed</Text><Text style={styles.body}>These events are loaded from the monitoring API. Push notifications are not configured for this application.</Text></Panel>
    {state === "loading" ? <LoadingState label="Loading recent activity…" /> : state === "error" ? <MessageState title="Activity unavailable" detail={error || "The monitoring service could not be reached."} action={<Text onPress={refresh} style={styles.link}>Refresh</Text>} /> : null}
    {!approvals.loading && approvals.error ? <Panel style={styles.authError}><Text style={styles.authErrorTitle}>Release activity unavailable</Text><Text style={styles.body}>{approvals.error}</Text></Panel> : null}
    {state === "empty" ? <MessageState title="No recent activity" detail="No monitoring alerts, deployment events or file activity are recorded." /> : null}
    {state === "content" && entries.map((entry) => <Panel key={entry.key} style={styles.entry}><View style={styles.row}><Text style={styles.kind}>{entry.kind}</Text><StatusPill status={entry.status} /></View><Text style={styles.title}>{entry.title}</Text>{entry.detail ? <Text style={styles.body}>{entry.detail}</Text> : null}<Text style={styles.time}>{formatDate(entry.time)}</Text></Panel>)}
  </ScrollView>;
}

const styles = StyleSheet.create({ content: { padding: 18, paddingBottom: 40, gap: 11 }, note: { backgroundColor: colors.blueSoft, borderColor: "#D5E2F5", gap: 6 }, noteTitle: { color: colors.blue, fontWeight: "700", fontSize: 14 }, authError: { backgroundColor: colors.amberSoft, borderColor: "#F1D99A", gap: 6 }, authErrorTitle: { color: colors.amber, fontWeight: "700", fontSize: 13 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 19 }, entry: { gap: 7 }, row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" }, kind: { color: colors.secondary, fontWeight: "700", fontSize: 11, textTransform: "uppercase" }, title: { color: colors.ink, fontSize: 14, fontWeight: "700" }, time: { color: colors.muted, fontSize: 11 }, link: { color: colors.blue, fontWeight: "700" }, });
