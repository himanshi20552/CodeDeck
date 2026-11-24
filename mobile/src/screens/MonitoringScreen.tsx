import React, { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { Project } from "../types";
import { formatDate, Label, LoadingState, MessageState, PageTitle, Panel, StatusPill } from "../components/common";

export default function MonitoringScreen({ projects }: { projects: Project[] }) {
  const [projectId, setProjectId] = useState<number | undefined>();
  const { data, loading, error, refresh } = useRemote(() => api.monitoring(projectId), [projectId]);
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Deployment monitoring" subtitle="Health, rollout state, pods, autoscaling and alerts from CodeDeck monitoring." />
    <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.filters}>
      <Filter title="All projects" selected={!projectId} onPress={() => setProjectId(undefined)} />
      {projects.map((project) => <Filter key={project.id} title={project.repository_name} selected={projectId === project.id} onPress={() => setProjectId(project.id)} />)}
    </ScrollView>
    {loading ? <LoadingState label="Loading monitoring snapshot…" /> : error ? <MessageState title="Monitoring data unavailable" detail={error} action={<Text onPress={refresh} style={styles.link}>Refresh</Text>} /> : null}
    {data ? <>
      <View style={styles.healthGrid}>{Object.entries(data.system_health || {}).map(([name, info]) => <Panel key={name} style={styles.health}><Label>{name.replaceAll("_", " ")}</Label><StatusPill status={info.status} /><Text style={styles.body}>{info.message || "Status received from backend"}</Text></Panel>)}</View>
      <Panel style={styles.snapshot}><View style={styles.row}><Text style={styles.title}>Kubernetes</Text><StatusPill status={data.kubernetes.status || "Unavailable"} /></View><Text style={styles.body}>{data.kubernetes.message || "No Kubernetes status message."}</Text><Text style={styles.meta}>Sampled {formatDate(data.kubernetes.sampled_at)}</Text></Panel>
      <Section title="Rollout health">
        {data.kubernetes.deployments?.length ? data.kubernetes.deployments.map((deployment, index) => <Panel key={`${deployment.namespace}-${deployment.name}-${index}`} style={styles.item}><View style={styles.row}><Text style={styles.itemTitle}>{deployment.name || deployment.target_name || "Deployment"}</Text><StatusPill status={deployment.status || deployment.health || "Unknown"} /></View><Text style={styles.body}>{deployment.namespace || "Namespace unavailable"} · {deployment.ready_replicas ?? "?"}/{deployment.replicas ?? "?"} ready</Text><Text style={styles.body}>Desired {deployment.desired_replicas ?? "?"} · Updated {deployment.updated_replicas ?? "?"}</Text></Panel>) : <Text style={styles.body}>No deployment rollout records in the current monitoring sample.</Text>}
      </Section>
      <Section title="Pods and restarts">
        {data.kubernetes.pods?.length ? data.kubernetes.pods.map((pod, index) => <Panel key={`${pod.namespace}-${pod.name}-${index}`} style={styles.item}><View style={styles.row}><Text numberOfLines={1} style={styles.itemTitle}>{pod.name || "Pod"}</Text><StatusPill status={pod.status || pod.phase || "Unknown"} /></View><Text style={styles.body}>{pod.namespace || "Namespace unavailable"} · Restarts {pod.restart_count ?? pod.restarts ?? 0}</Text>{pod.reason ? <Text style={[styles.body, { color: colors.red }]}>{pod.reason}</Text> : null}</Panel>) : <Text style={styles.body}>No pod records in the current monitoring sample.</Text>}
      </Section>
      <Section title="Autoscaling">
        {data.kubernetes.hpas?.length ? data.kubernetes.hpas.map((hpa, index) => <Panel key={`${hpa.namespace}-${hpa.name}-${index}`} style={styles.item}><View style={styles.row}><Text style={styles.itemTitle}>{hpa.name || hpa.target_name || "Horizontal Pod Autoscaler"}</Text><StatusPill status={hpa.status || hpa.state || "Available"} /></View><Text style={styles.body}>Replicas {hpa.current_replicas ?? "?"} current · {hpa.desired_replicas ?? "?"} desired</Text><Text style={styles.body}>Range {hpa.min_replicas ?? "?"}–{hpa.max_replicas ?? "?"}</Text><Text style={styles.body}>CPU {hpa.current_cpu_utilization ?? "Unavailable"}% / target {hpa.target_cpu_utilization ?? "Unavailable"}%</Text></Panel>) : <Text style={styles.body}>No HPA resources were reported for this filter.</Text>}
      </Section>
      <Section title="Resource metrics">
        <Panel style={styles.item}><View style={styles.row}><Text style={styles.itemTitle}>Metrics Server</Text><StatusPill status={data.kubernetes.metrics?.status || "Unavailable"} /></View><Text style={styles.body}>{data.kubernetes.metrics?.message || "CPU and memory metrics were not included in the sample."}</Text>{data.kubernetes.metrics?.pods?.map((pod, index) => <Text key={index} style={styles.body}>{pod.name || "Pod"}: CPU {pod.cpu || "Unavailable"} · Memory {pod.memory || "Unavailable"}</Text>)}</Panel>
      </Section>
      <Section title="Recent alerts">
        {data.alerts?.length ? data.alerts.map((alert) => <Panel key={alert.id || alert.fingerprint} style={styles.item}><View style={styles.row}><Text style={styles.itemTitle}>{alert.title || alert.type || "Alert"}</Text><StatusPill status={alert.severity || "info"} /></View><Text style={styles.body}>{alert.message || "No alert description."}</Text><Text style={styles.meta}>Last seen {formatDate(alert.last_seen)} · {alert.occurrence_count || 1} occurrence(s)</Text></Panel>) : <Text style={styles.body}>No alerts are recorded for this selection.</Text>}
      </Section>
    </> : null}
  </ScrollView>;
}

function Filter({ title, selected, onPress }: { title: string; selected: boolean; onPress: () => void }) { return <Pressable onPress={onPress} style={[styles.filter, selected && styles.filterSelected]}><Text style={[styles.filterText, selected && { color: "#FFFFFF" }]}>{title}</Text></Pressable>; }
function Section({ title, children }: { title: string; children: React.ReactNode }) { return <View style={styles.section}><Text style={styles.sectionTitle}>{title}</Text>{children}</View>; }

const styles = StyleSheet.create({ content: { padding: 18, paddingBottom: 42, gap: 13 }, filters: { gap: 7 }, filter: { borderRadius: 8, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, paddingVertical: 9, paddingHorizontal: 12 }, filterSelected: { backgroundColor: colors.blue, borderColor: colors.blue }, filterText: { color: colors.secondary, fontSize: 12, fontWeight: "700" }, healthGrid: { gap: 8 }, health: { gap: 8 }, snapshot: { gap: 8 }, title: { color: colors.ink, fontSize: 15, fontWeight: "700" }, row: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", gap: 8 }, body: { color: colors.secondary, fontSize: 13, lineHeight: 19 }, meta: { color: colors.muted, fontSize: 11 }, section: { gap: 9, marginTop: 5 }, sectionTitle: { color: colors.ink, fontSize: 16, fontWeight: "700" }, item: { gap: 7 }, itemTitle: { flex: 1, color: colors.ink, fontSize: 14, fontWeight: "700" }, link: { color: colors.blue, fontWeight: "700" }, });
