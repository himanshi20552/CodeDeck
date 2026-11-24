import React from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { Approval } from "../types";
import { formatDate, Label, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

export default function ApprovalInboxScreen({ onReview }: { onReview: (id: number) => void }) {
  const { data, loading, error, refresh } = useRemote(api.pendingApprovals);
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Approval inbox" subtitle="Review release candidates after staging verification." />
    {data?.mode === "demo" ? <Panel style={styles.demoNotice}><Text style={styles.authTitle}>Local demo mode</Text><Text style={styles.body}>Actions are saved for demonstration, use a fixed unverified identity and never start a production deployment.</Text></Panel> : null}
    {data?.mode === "token" ? <Panel style={styles.authNotice}><Text style={styles.authTitle}>Service authentication</Text><Text style={styles.body}>This server is using its configured approval credential. Manager identity verification depends on the connected authentication provider.</Text></Panel> : null}
    {loading ? <LoadingState label="Loading pending release requests…" /> : error ? <MessageState title="Approval requests could not be loaded" detail={error} action={<Text onPress={refresh} style={styles.link}>Try again</Text>} /> : null}
    {!loading && !error && data?.items.length === 0 ? <MessageState title="Inbox is clear" detail="There are no releases awaiting approval." /> : null}
    {data?.items.map((approval) => <ApprovalCard key={approval.id} approval={approval} onPress={() => onReview(approval.id)} />)}
    <Text style={styles.footnote}>Approval decisions use the backend's configured authentication mode. This app never stores a backend service token.</Text>
  </ScrollView>;
}

function ApprovalCard({ approval, onPress }: { approval: Approval; onPress: () => void }) {
  const stage = approval.summary?.staging;
  const requested = approval.requested_at ? new Date(approval.requested_at).getTime() : NaN;
  const waiting = Number.isFinite(requested) ? Math.max(0, Math.floor((Date.now() - requested) / 60000)) : null;
  return <Pressable onPress={onPress} accessibilityRole="button" style={({ pressed }) => [styles.card, pressed && { opacity: 0.85 }]}>
    <View style={styles.top}><View style={{ flex: 1 }}><Text style={styles.project}>{approval.project?.name || `Project ${approval.project_id}`}</Text><Text style={styles.release}>Release {approval.release_version || approval.commit_id || `#${approval.id}`}</Text></View><StatusPill status={approval.status} /></View>
    <Text style={styles.body}>{approval.branch || "Branch unavailable"}{approval.commit_id ? ` · ${approval.commit_id.slice(0, 9)}` : ""}</Text>
    <View style={styles.row}><View><Label>Requested</Label><Text style={styles.value}>{formatDate(approval.requested_at)}</Text></View><View><Label>Waiting</Label><Text style={styles.value}>{waiting == null ? "Not recorded" : `${waiting} min`}</Text></View></View>
    <View style={styles.row}><View><Label>Staging</Label><Text style={styles.value}>{stage?.status || approval.staging_deployment?.status || "Not recorded"}</Text></View><View><Label>Smoke test</Label><Text style={styles.value}>{stage?.smoke_test?.status || "Not recorded"}</Text></View></View>
    <Text style={styles.link}>Review release  ›</Text>
  </Pressable>;
}

const styles = StyleSheet.create({
  content: { padding: 18, paddingBottom: 40, gap: 12 }, authNotice: { backgroundColor: colors.blueSoft, borderColor: "#D5E2F5", gap: 7 }, demoNotice: { backgroundColor: colors.amberSoft, borderColor: "#F1D99A", gap: 7 }, authTitle: { color: colors.blue, fontWeight: "700", fontSize: 14 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 19 }, card: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 12, padding: 15, gap: 11 }, top: { flexDirection: "row", alignItems: "flex-start", gap: 10 }, project: { color: colors.ink, fontWeight: "700", fontSize: 16 }, release: { color: colors.secondary, fontSize: 13, marginTop: 3 }, row: { flexDirection: "row", justifyContent: "space-between", gap: 12 }, value: { color: colors.ink, fontSize: 13, marginTop: 4 }, link: { color: colors.blue, fontSize: 13, fontWeight: "700", paddingVertical: 4 }, footnote: { color: colors.secondary, fontSize: 12, lineHeight: 18, marginVertical: 4 },
});
