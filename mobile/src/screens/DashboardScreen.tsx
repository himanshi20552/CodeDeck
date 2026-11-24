import React, { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { Approval, Project, Workflow } from "../types";
import { formatDuration, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

type Props = { onProject: (project: Project) => void; onApproval: (id: number) => void; onApprovals: () => void };

export default function DashboardScreen({ onProject, onApproval, onApprovals }: Props) {
  const projects = useRemote(api.projects);
  const [workflows, setWorkflows] = useState<Record<number, Workflow>>({});
  const [flowError, setFlowError] = useState<string | null>(null);
  const [flowsLoading, setFlowsLoading] = useState(false);
  const [pending, setPending] = useState<Approval[] | null>(null);
  const [approvalError, setApprovalError] = useState<string | null>(null);

  useEffect(() => {
    if (!projects.data?.length) { setWorkflows({}); return; }
    let active = true;
    setFlowsLoading(true); setFlowError(null);
    Promise.all(projects.data.map(async (project) => [project.id, await api.workflow(project.id)] as const))
      .then((items) => { if (active) setWorkflows(Object.fromEntries(items)); })
      .catch((error: unknown) => { if (active) setFlowError(error instanceof Error ? error.message : "Workflow data could not be loaded."); })
      .finally(() => { if (active) setFlowsLoading(false); });
    return () => { active = false; };
  }, [projects.data]);

  useEffect(() => {
    let active = true;
    api.pendingApprovals().then((response) => { if (active) setPending(response.items); })
      .catch((error: unknown) => { if (active) setApprovalError(error instanceof Error ? error.message : "Approval inbox unavailable."); });
    return () => { active = false; };
  }, []);

  if (projects.loading) return <LoadingState />;
  if (projects.error) return <MessageState title="Could not load projects" detail={projects.error} />;
  const rows = projects.data || [];
  const workflowsDone = Object.values(workflows);
  const attention = rows.filter((project) => /failed|blocked|crash/i.test(project.status)).length;
  const activeCount = rows.filter((project) => /running|deploying|analyzing|staging|awaiting_approval/i.test(project.status)).length;

  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Manager overview" subtitle="Release progress and delivery health from your CodeDeck workspace." />
    <View style={styles.metrics}>
      <Metric label="Projects" value={String(rows.length)} />
      <Metric label="In progress" value={String(activeCount)} />
      <Pressable onPress={onApprovals} style={styles.metricPress}><Metric label="Awaiting approval" value={pending ? String(pending.length) : "—"} /></Pressable>
      <Metric label="Needs attention" value={String(attention)} warn={attention > 0} />
    </View>
    {approvalError ? <Panel style={styles.notice}><Text style={styles.noticeTitle}>Approval inbox access is not configured</Text><Text style={styles.body}>{approvalError}</Text><Text style={styles.body}>The mobile app does not contain the backend service token. User authentication must be connected before managers can review decisions here.</Text></Panel> : null}
    <View style={styles.sectionHeader}><Text style={styles.sectionTitle}>Delivery portfolio</Text><Text style={styles.sectionMeta}>{flowsLoading ? "Refreshing workflows…" : `${workflowsDone.length} workflow records`}</Text></View>
    {flowError ? <MessageState title="Some workflow data could not be loaded" detail={flowError} /> : null}
    {rows.length === 0 ? <MessageState title="No projects yet" detail="Projects will appear here after they are connected in CodeDeck." /> : rows.map((project) => {
      const workflow = workflows[project.id];
      const stages = workflow?.stages || [];
      const completed = stages.filter((stage) => stage.status === "Completed").length;
      const active = stages.find((stage) => stage.status === "Running" || stage.status === "Queued" || stage.status === "Awaiting Approval");
      const elapsed = active?.started_at ? Math.max(0, (Date.now() - new Date(active.started_at).getTime()) / 1000) : null;
      return <Pressable key={project.id} accessibilityRole="button" onPress={() => onProject(project)} style={({ pressed }) => [styles.projectCard, pressed && { opacity: 0.85 }]}>
        <View style={styles.projectTop}><View style={{ flex: 1 }}><Text style={styles.projectName}>{project.repository_name}</Text><Text style={styles.repo}>{project.repository_url}</Text></View><StatusPill status={project.status.replaceAll("_", " ")} /></View>
        {workflow ? <>
          <View style={styles.progressTrack}><View style={[styles.progressFill, { width: `${Math.max(0, Math.min(100, stages.length ? completed / stages.length * 100 : 0))}%` }]} /></View>
          <View style={styles.projectFoot}><Text style={styles.body}>{completed} of {stages.length} stages completed</Text><Text style={styles.body}>{active ? `${active.name} · ${formatDuration(elapsed)}` : workflow.release.branch || "Workflow idle"}</Text></View>
        </> : <Text style={styles.body}>{flowsLoading ? "Loading workflow…" : "Workflow details unavailable"}</Text>}
      </Pressable>;
    })}
    {pending?.length ? <>
      <View style={styles.sectionHeader}><Text style={styles.sectionTitle}>Release requests</Text><Pressable onPress={onApprovals}><Text style={styles.link}>View inbox</Text></Pressable></View>
      {pending.slice(0, 3).map((approval) => <Pressable key={approval.id} onPress={() => onApproval(approval.id)} style={styles.approvalRow}><View style={{ flex: 1 }}><Text style={styles.projectName}>{approval.project?.name || `Project ${approval.project_id}`}</Text><Text style={styles.repo}>Release {approval.release_version || approval.commit_id || `#${approval.id}`}</Text></View><StatusPill status={approval.status} /></Pressable>)}
    </> : null}
  </ScrollView>;
}

function Metric({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return <View style={styles.metric}><Text style={[styles.metricValue, warn && { color: colors.red }]}>{value}</Text><Text style={styles.metricLabel}>{label}</Text></View>;
}

const styles = StyleSheet.create({
  content: { padding: 18, paddingBottom: 36, gap: 14 }, metrics: { flexDirection: "row", flexWrap: "wrap", gap: 9 }, metric: { flex: 1, minWidth: "46%", backgroundColor: colors.surface, borderColor: colors.border, borderWidth: 1, borderRadius: 11, padding: 13 }, metricPress: { flex: 1, minWidth: "46%" }, metricValue: { fontSize: 24, fontWeight: "700", color: colors.ink }, metricLabel: { color: colors.secondary, fontSize: 12, marginTop: 4 },
  sectionHeader: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", marginTop: 8 }, sectionTitle: { fontSize: 17, color: colors.ink, fontWeight: "700" }, sectionMeta: { color: colors.secondary, fontSize: 12 },
  projectCard: { padding: 15, backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 12, gap: 13 }, projectTop: { flexDirection: "row", alignItems: "flex-start", gap: 10 }, projectName: { color: colors.ink, fontSize: 15, fontWeight: "700" }, repo: { color: colors.secondary, fontSize: 12, marginTop: 4 }, progressTrack: { height: 6, backgroundColor: colors.slateSoft, borderRadius: 5, overflow: "hidden" }, progressFill: { height: 6, backgroundColor: colors.blue, borderRadius: 5 }, projectFoot: { flexDirection: "row", flexWrap: "wrap", justifyContent: "space-between", gap: 5 }, body: { color: colors.secondary, fontSize: 12, lineHeight: 18 },
  notice: { backgroundColor: colors.amberSoft, borderColor: "#F1D99A", gap: 8 }, noticeTitle: { color: colors.amber, fontSize: 14, fontWeight: "700" }, approvalRow: { flexDirection: "row", alignItems: "center", backgroundColor: colors.surface, borderColor: colors.border, borderWidth: 1, borderRadius: 10, padding: 13, gap: 12 }, link: { color: colors.blue, fontWeight: "700", fontSize: 13 },
});
