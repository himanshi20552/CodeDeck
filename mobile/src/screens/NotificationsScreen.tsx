import React from "react";
import { ScrollView, StyleSheet, Text } from "react-native";
import { hasAuthProvider } from "../api/auth";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { formatDate, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

export default function NotificationsScreen() {
  const { data, loading, error, refresh } = useRemote(() => api.monitoring());
  const approvals = useRemote(() => hasAuthProvider() ? api.approvalHistory() : Promise.resolve({ items: [], count: 0 }));
  const entries = [
    ...(data?.alerts || []).map((item) => ({ key: `alert-${item.id}`, kind: "Alert", title: item.title || item.type || "CodeDeck alert", detail: item.message || "", time: item.last_seen, status: item.severity })),
    ...(data?.deployment_events || []).map((item) => ({ key: `deployment-${item.id}`, kind: "Deployment", title: `${item.environment} ${item.version} · ${item.status}`, detail: item.logs?.length ? item.logs[item.logs.length - 1]?.message || item.deployment_type : item.deployment_type, time: item.created_at, status: item.status })),
    ...(data?.file_activity || []).map((item) => ({ key: `file-${item.id}`, kind: "Repository", title: `${item.event_type} · ${item.path}`, detail: item.summary || item.workflow_context, time: item.occurred_at, status: "Recorded" })),
    ...(approvals.data?.items || []).flatMap((approval) => (approval.events || []).map((event) => ({ key: `approval-${event.id}`, kind: "Release", title: `${approval.project?.name || `Project ${approval.project_id}`} · ${event.to_status}`, detail: event.comments || approval.commit_message || `Release ${approval.release_version || approval.id}`, time: event.occurred_at, status: event.to_status }))),
  ].sort((a, b) => new Date(b.time || 0).getTime() - new Date(a.time || 0).getTime());
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Activity" subtitle="Recent alerts, deployments and repository events reported by CodeDeck." />
    <Panel style={styles.note}><Text style={styles.noteTitle}>In-app activity feed</Text><Text style={styles.body}>These events are loaded from the monitoring API. Push notifications are not configured for this application.</Text></Panel>
    {loading ? <LoadingState label="Loading recent activity…" /> : error ? <MessageState title="Activity unavailable" detail={error} action={<Text onPress={refresh} style={styles.link}>Refresh</Text>} /> : null}
    {!loading && !error && entries.length === 0 ? <MessageState title="No recent activity" detail="No monitoring alerts, deployment events or file activity are recorded." /> : null}
    {entries.map((entry) => <Panel key={entry.key} style={styles.entry}><View style={styles.row}><Text style={styles.kind}>{entry.kind}</Text><StatusPill status={entry.status} /></View><Text style={styles.title}>{entry.title}</Text>{entry.detail ? <Text style={styles.body}>{entry.detail}</Text> : null}<Text style={styles.time}>{formatDate(entry.time)}</Text></Panel>)}
  </ScrollView>;
}

const styles = StyleSheet.create({ content: { padding: 18, paddingBottom: 40, gap: 11 }, note: { backgroundColor: colors.blueSoft, borderColor: "#D5E2F5", gap: 6 }, noteTitle: { color: colors.blue, fontWeight: "700", fontSize: 14 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 19 }, entry: { gap: 7 }, row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" }, kind: { color: colors.secondary, fontWeight: "700", fontSize: 11, textTransform: "uppercase" }, title: { color: colors.ink, fontSize: 14, fontWeight: "700" }, time: { color: colors.muted, fontSize: 11 }, link: { color: colors.blue, fontWeight: "700" }, });
