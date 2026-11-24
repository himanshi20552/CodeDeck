import React from "react";
import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { api } from "../api/client";
import { useRemote } from "../api/useRemote";
import { colors } from "../theme";
import { Project } from "../types";
import { LoadingState, MessageState, PageTitle, StatusPill } from "../components/common";

export default function ProjectsScreen({ onProject }: { onProject: (project: Project) => void }) {
  const { data, loading, error, refresh } = useRemote(api.projects);
  return <ScrollView contentContainerStyle={styles.content}>
    <PageTitle title="Projects" subtitle="Projects registered with the shared CodeDeck backend." />
    {loading ? <LoadingState /> : error ? <MessageState title="Projects unavailable" detail={error} action={<Text onPress={refresh} style={styles.link}>Try again</Text>} /> : null}
    {!loading && !error && !data?.length ? <MessageState title="No projects registered" detail="New projects connected through the developer application will appear here." /> : null}
    {data?.map((project) => <Pressable key={project.id} onPress={() => onProject(project)} style={({ pressed }) => [styles.item, pressed && { opacity: 0.82 }]}>
      <View style={styles.row}><Text style={styles.name}>{project.repository_name}</Text><StatusPill status={project.status.replaceAll("_", " ")} /></View>
      <Text style={styles.meta}>Connected {new Date(project.created_at).toLocaleDateString()}</Text>
      <Text style={styles.link}>View workflow  ›</Text>
    </Pressable>)}
  </ScrollView>;
}

const styles = StyleSheet.create({ content: { padding: 18, paddingBottom: 40, gap: 12 }, item: { borderWidth: 1, borderColor: colors.border, borderRadius: 12, padding: 15, backgroundColor: colors.surface, gap: 10 }, row: { flexDirection: "row", alignItems: "center", gap: 10 }, name: { color: colors.ink, fontWeight: "700", fontSize: 16 }, url: { color: colors.secondary, fontSize: 12, marginTop: 4 }, meta: { color: colors.secondary, fontSize: 12 }, link: { color: colors.blue, fontWeight: "700", fontSize: 13 }, });
