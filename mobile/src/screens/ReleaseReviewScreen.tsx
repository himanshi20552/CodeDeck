import React, { useState } from "react";
import { Alert, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { AUTH_NOT_CONFIGURED, hasAuthProvider } from "../api/auth";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { formatDate, Label, LoadingState, MessageState, PageTitle, Panel, Field, StatusPill, ActionButton } from "../components/common";

export default function ReleaseReviewScreen({ approvalId }: { approvalId: number }) {
  const { data, loading, error, refresh, setData } = useRemote(() => api.approval(approvalId), [approvalId]);
  const [comments, setComments] = useState("");
  const [busy, setBusy] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const decide = (decision: "approve" | "reject") => {
    Alert.alert(decision === "approve" ? "Approve this release?" : "Reject this release?",
      decision === "approve" ? "CodeDeck will queue the production rollout after your decision is recorded." : "This release will be marked rejected and will not be queued for production.",
      [{ text: "Cancel", style: "cancel" }, { text: decision === "approve" ? "Approve release" : "Reject release", style: decision === "reject" ? "destructive" : "default", onPress: async () => {
        setBusy(true); setDecisionError(null);
        try {
          const result = await api.decide(approvalId, decision, comments.trim());
          setData(result);
          Alert.alert("Decision recorded", `${result.status}${result.decision_timestamp ? ` at ${formatDate(result.decision_timestamp)}` : ""}`);
        } catch (reason) {
          const message = reason instanceof Error ? reason.message : "The decision could not be submitted.";
          setDecisionError(message); Alert.alert("Decision not submitted", message);
        } finally { setBusy(false); }
      } }]);
  };

  if (loading) return <ScrollView contentContainerStyle={styles.content}><LoadingState label="Loading release review…" /></ScrollView>;
  if (error || !data) return <ScrollView contentContainerStyle={styles.content}><MessageState title="Release review unavailable" detail={error || "The server returned no release record."} action={<Text onPress={refresh} style={styles.link}>Try again</Text>} /></ScrollView>;
  const summary = data.summary || {};
  const changes = summary.what_changed || {};
  const stage = summary.staging || {};
  const tests = summary.tests || {};
  const security = summary.security || {};
  const canDecide = data.status === "Awaiting Approval" && !data.decision;
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title={data.project?.name || `Release ${data.id}`} subtitle={data.commit_message || "Release candidate review"} />
    <Panel style={styles.summary}>
      <View style={styles.row}><Text style={styles.heading}>Release {data.release_version || data.commit_id || `#${data.id}`}</Text><StatusPill status={data.status} /></View>
      <Fact label="Branch" value={data.branch || "Not recorded"} /><Fact label="Commit" value={data.commit_id || "Not recorded"} />
      <Fact label="Requested" value={formatDate(data.requested_at)} /><Fact label="Requested by" value={`${data.requested_by || "Not recorded"}${data.requester_identity_verified ? " · verified" : " · identity unverified"}`} />
    </Panel>
    <Block title="Change summary"><Text style={styles.body}>{changes.summary || "No change summary was recorded."}</Text><Text style={styles.body}>{changes.comparison_available ? "Git comparison is available." : "Only the current repository snapshot is available; historic file changes were not captured."}</Text>
      <View style={styles.changeGrid}><List title={`Added (${changes.added?.length || 0})`} items={changes.added} /><List title={`Modified (${changes.modified?.length || 0})`} items={changes.modified} /><List title={`Deleted (${changes.deleted?.length || 0})`} items={changes.deleted} /></View>
    </Block>
    <Block title="Staging verification"><View style={styles.row}><Label>Deployment</Label><StatusPill status={stage.status || "Not recorded"} /></View><Text style={styles.body}>Smoke test: {stage.smoke_test?.status || "Not recorded"}</Text><Text style={styles.body}>Environment: {stage.environment || "Not recorded"}{stage.namespace ? ` · Namespace ${stage.namespace}` : ""}</Text></Block>
    <Block title="Test and security results"><Result label="Automated tests" value={tests.status || "Not recorded"} detail={tests.summary || tests.message || "No test summary was recorded."} /><Result label="Security" value={security.result?.status || (security.score == null ? "Not recorded" : `Score ${security.score}`)} detail={security.result?.summary || `${security.high_severity_findings?.length || 0} high-severity findings recorded.`} /></Block>
    <Block title="Dependency changes"><Text selectable style={styles.body}>{JSON.stringify(summary.dependencies || [], null, 2)}</Text></Block>
    <Block title="Release risk"><Text style={styles.body}>{(summary.deployment_risks || []).length ? summary.deployment_risks.map((risk: string) => `• ${risk}`).join("\n") : "No persisted risk indicators were reported."}</Text></Block>
    <Block title="Rollback"><Text style={styles.body}>{summary.rollback?.description || "Rollback information is not recorded."}</Text><Text style={styles.body}>Available: {summary.rollback?.available ? "Yes" : "Not confirmed"}</Text></Block>
    {data.production_deployment ? <Block title="Production deployment"><View style={styles.row}><Text style={styles.body}>{data.production_deployment.environment} · {data.production_deployment.version}</Text><StatusPill status={data.production_deployment.status} /></View><Text style={styles.body}>{data.production_deployment.failure_reason || `Deployment ${data.production_deployment.id}`}</Text></Block> : null}
    {canDecide ? <Block title="Decision comments"><Field value={comments} onChangeText={setComments} placeholder="Add review notes (optional)" multiline />{!hasAuthProvider() ? <Text style={styles.unverified}>{AUTH_NOT_CONFIGURED}</Text> : null}{decisionError ? <Text style={styles.error}>{decisionError}</Text> : null}<View style={styles.actions}><View style={{ flex: 1 }}><ActionButton title="Reject" variant="danger" disabled={busy || !hasAuthProvider()} onPress={() => decide("reject")} /></View><View style={{ flex: 1 }}><ActionButton title="Approve release" disabled={busy || !hasAuthProvider()} onPress={() => decide("approve")} /></View></View></Block> : <Panel><Text style={styles.body}>This release no longer accepts a decision. Current decision: {data.decision || "none"}.</Text>{data.comments ? <Text style={styles.body}>Comments: {data.comments}</Text> : null}</Panel>}
  </ScrollView>;
}

function Block({ title, children }: { title: string; children: React.ReactNode }) { return <View style={styles.block}><Text style={styles.blockTitle}>{title}</Text><Panel style={styles.blockPanel}>{children}</Panel></View>; }
function Fact({ label, value }: { label: string; value: string }) { return <View style={styles.fact}><Label>{label}</Label><Text selectable style={styles.body}>{value}</Text></View>; }
function List({ title, items }: { title: string; items?: string[] }) { return <View style={styles.list}><Text style={styles.listTitle}>{title}</Text>{items?.length ? items.slice(0, 8).map((item) => <Text selectable key={item} style={styles.file}>{item}</Text>) : <Text style={styles.body}>None recorded</Text>}</View>; }
function Result({ label, value, detail }: { label: string; value: string; detail: string }) { return <View style={styles.result}><View style={styles.row}><Text style={styles.heading}>{label}</Text><StatusPill status={value} /></View><Text style={styles.body}>{detail}</Text></View>; }

const styles = StyleSheet.create({
  content: { padding: 18, paddingBottom: 44, gap: 12 }, summary: { gap: 12 }, row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 8 }, heading: { flex: 1, color: colors.ink, fontSize: 15, fontWeight: "700" }, fact: { gap: 4 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 20 }, block: { gap: 8, marginTop: 5 }, blockTitle: { color: colors.ink, fontSize: 16, fontWeight: "700" }, blockPanel: { gap: 9 }, changeGrid: { gap: 8, marginTop: 6 }, list: { gap: 5 }, listTitle: { color: colors.ink, fontWeight: "700", fontSize: 12 }, file: { color: colors.secondary, fontFamily: "monospace", fontSize: 11 }, result: { gap: 6, paddingBottom: 7 }, actions: { flexDirection: "row", gap: 10, marginTop: 5 }, unverified: { color: colors.amber, fontSize: 12, lineHeight: 18 }, error: { color: colors.red, fontSize: 13 }, link: { color: colors.blue, fontWeight: "700", padding: 6 },
});
