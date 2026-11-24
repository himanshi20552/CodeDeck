import React from "react";
import { ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { Project, WorkflowStage } from "../types";
import { formatDate, formatDuration, Label, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

export default function ProjectDetailsScreen({ project }: { project: Project }) {
  const workflow = useRemote(() => api.workflow(project.id), [project.id]);
  const analysis = useRemote(() => api.analysis(project.id), [project.id]);
  if (workflow.loading) return <ScrollView contentContainerStyle={styles.content}><LoadingState /></ScrollView>;
  if (workflow.error || !workflow.data) return <ScrollView contentContainerStyle={styles.content}><MessageState title="Project workflow unavailable" detail={workflow.error || "No workflow record was returned."} /></ScrollView>;
  const data = workflow.data;
  const result = analysis.data?.analysis_result || {};
  const test = result.test_result || {};
  const security = result.security_result || {};
  const snapshot = data.repository_snapshot || {};
  const current = data.stages.find((stage) => ["Running", "Queued", "Blocked", "Failed"].includes(stage.status));
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title={project.repository_name} subtitle={project.repository_url} />
    <Panel style={styles.overview}>
      <View style={styles.row}><Label>Project status</Label><StatusPill status={project.status.replaceAll("_", " ")} /></View>
      <Text style={styles.heading}>{current ? current.name : "Workflow overview"}</Text>
      <Text style={styles.body}>{current?.output || `Release ${data.release.status.replaceAll("_", " ")}`}</Text>
      <Text style={styles.body}>Current stage duration: {current?.started_at ? formatDuration(Math.max(0, (Date.now() - new Date(current.started_at).getTime()) / 1000)) : "Not recorded"}</Text>
      <Text style={styles.body}>Workflow duration: {formatDuration(data.analytics.total_duration_seconds)}</Text>
    </Panel>
    <Section title="Delivery workflow">
      {data.stages.map((stage, index) => <Stage key={stage.id} stage={stage} last={index === data.stages.length - 1} />)}
    </Section>
    <Section title="Repository and release">
      <Panel style={styles.panelContent}>
        <Fact label="Branch" value={snapshot.branch || "Not captured"} />
        <Fact label="Commit" value={snapshot.commit_id || "Not captured"} />
        <Fact label="Commit message" value={snapshot.commit_message || "Not recorded"} />
        <Fact label="Environment" value={data.release.environment || "Not recorded"} />
        <Fact label="Version" value={data.release.version || "Not recorded"} />
        <Fact label="Release approval" value={data.release.approval_status || "Not recorded"} />
        <Fact label="Change summary" value={snapshot.change_summary || (snapshot.comparison_available ? "Git comparison available" : "Only the current repository snapshot is available; historical Git changes were not captured.")} />
        <Fact label="Files in snapshot" value={snapshot.file_count == null ? "Not recorded" : String(snapshot.file_count)} />
      </Panel>
      <View style={styles.changeGrid}>
        <ChangeList title="Added" values={snapshot.added || []} tone="teal" />
        <ChangeList title="Modified" values={snapshot.modified || []} tone="blue" />
        <ChangeList title="Deleted" values={snapshot.deleted || []} tone="red" />
      </View>
    </Section>
    <Section title="Validation results">
      {!analysis.data && analysis.loading ? <LoadingState label="Loading analysis results…" /> : analysis.error ? <MessageState title="Analysis results unavailable" detail={analysis.error} /> : <View style={styles.results}>
        <ResultPanel title="Automated tests" status={test.status || "Not recorded"} detail={test.summary || test.message || test.command || "No persisted test summary."} />
        <ResultPanel title="Security checks" status={security.status || (analysis.data?.security_score != null ? "Scored" : "Not recorded")} detail={security.summary || (analysis.data?.security_score != null ? `Security score: ${analysis.data.security_score}` : "No persisted security result.")} />
        <Panel><Label>Dependencies</Label><Text style={styles.body}>{JSON.stringify(result.staging_dependencies || result.dependencies || [], null, 2)}</Text></Panel>
      </View>}
    </Section>
    <Section title="Deployment history">
      {data.deployment_history.length === 0 ? <Text style={styles.body}>No deployment history is recorded.</Text> : data.deployment_history.map((deployment) => <Panel key={deployment.id} style={styles.deployment}>
        <View style={styles.row}><Text style={styles.heading}>{deployment.environment} · {deployment.version}</Text><StatusPill status={deployment.status} /></View>
        <Text style={styles.body}>{deployment.deployment_type.replaceAll("_", " ")} · {formatDate(deployment.created_at)}</Text>
        {deployment.details?.failure_reason ? <Text style={[styles.body, { color: colors.red }]}>{deployment.details.failure_reason}</Text> : null}
      </Panel>)}
    </Section>
  </ScrollView>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) { return <View style={styles.section}><Text style={styles.sectionTitle}>{title}</Text>{children}</View>; }
function Fact({ label, value }: { label: string; value: string }) { return <View style={styles.fact}><Text style={styles.factLabel}>{label}</Text><Text selectable style={styles.factValue}>{value}</Text></View>; }
function ChangeList({ title, values, tone }: { title: string; values: string[]; tone: "teal" | "blue" | "red" }) {
  const color = tone === "teal" ? colors.teal : tone === "red" ? colors.red : colors.blue;
  return <Panel style={styles.changeCard}><Text style={[styles.changeTitle, { color }]}>{title} · {values.length}</Text>{values.length ? values.slice(0, 8).map((file) => <Text selectable numberOfLines={2} key={file} style={styles.file}>{file}</Text>) : <Text style={styles.body}>No files recorded</Text>}</Panel>;
}
function ResultPanel({ title, status, detail }: { title: string; status: string; detail: string }) { return <Panel style={styles.resultPanel}><View style={styles.row}><Text style={styles.heading}>{title}</Text><StatusPill status={status} /></View><Text style={styles.body}>{detail}</Text></Panel>; }
function Stage({ stage, last }: { stage: WorkflowStage; last: boolean }) {
  return <View style={styles.stageRow}><View style={styles.timeline}><View style={[styles.dot, stage.status === "Completed" && { backgroundColor: colors.teal }, stage.status === "Failed" && { backgroundColor: colors.red }, stage.status === "Running" && { backgroundColor: colors.blue }]} />{!last ? <View style={styles.line} /> : null}</View><View style={[styles.stageBody, last && { paddingBottom: 0 }]}>
    <View style={styles.row}><Text style={styles.heading}>{stage.name}</Text><StatusPill status={stage.status} /></View>
    <Text style={styles.body}>{stage.actor || "Responsible actor not recorded"} · Duration {formatDuration(stage.duration_seconds)}</Text>
    <Text style={styles.body}>Waiting {formatDuration(stage.waiting_seconds)} · Execution {formatDuration(stage.execution_seconds)}</Text>
    <Text style={styles.body}>Started {formatDate(stage.started_at)} · Completed {formatDate(stage.completed_at)}</Text>
    {stage.dependencies.length ? <Text style={styles.muted}>Depends on {stage.dependencies.join(", ")}</Text> : null}
    {stage.output ? <Text style={styles.stageOutput}>{stage.output}</Text> : null}
    {stage.logs?.slice(0, 3).map((entry, i) => <Text key={i} style={styles.log}>{typeof entry === "string" ? entry : entry.message || JSON.stringify(entry)}</Text>)}
  </View></View>;
}

const styles = StyleSheet.create({
  content: { padding: 18, paddingBottom: 38, gap: 14 }, overview: { gap: 9 }, row: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 8 }, heading: { flex: 1, color: colors.ink, fontWeight: "700", fontSize: 14 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 19 }, muted: { color: colors.muted, fontSize: 12 },
  section: { gap: 10, marginTop: 5 }, sectionTitle: { fontSize: 17, fontWeight: "700", color: colors.ink }, panelContent: { gap: 12 }, fact: { gap: 3 }, factLabel: { color: colors.secondary, fontSize: 11, fontWeight: "700", textTransform: "uppercase" }, factValue: { color: colors.ink, fontSize: 13, lineHeight: 19 }, changeGrid: { gap: 8 }, changeCard: { gap: 8 }, changeTitle: { fontSize: 13, fontWeight: "700" }, file: { color: colors.ink, fontSize: 12, fontFamily: "monospace" },
  results: { gap: 8 }, resultPanel: { gap: 9 }, deployment: { gap: 8 },
  stageRow: { flexDirection: "row", minHeight: 100 }, timeline: { width: 22, alignItems: "center" }, dot: { width: 11, height: 11, borderRadius: 6, backgroundColor: colors.muted, marginTop: 5, zIndex: 1 }, line: { width: 1, flex: 1, backgroundColor: colors.border, marginTop: -1 }, stageBody: { flex: 1, paddingBottom: 17, gap: 5 }, stageOutput: { color: colors.ink, fontSize: 12, lineHeight: 18, backgroundColor: colors.surfaceMuted, padding: 8, borderRadius: 7 }, log: { color: colors.secondary, fontSize: 11, lineHeight: 16 },
});
