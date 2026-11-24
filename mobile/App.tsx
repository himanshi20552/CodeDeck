import React, { useState } from "react";
import { Pressable, SafeAreaView, StatusBar, StyleSheet, Text, View } from "react-native";
import { api } from "./src/api/client";
import { useRemote } from "./src/api/useRemote";
import DashboardScreen from "./src/screens/DashboardScreen";
import ProjectsScreen from "./src/screens/ProjectsScreen";
import ProjectDetailsScreen from "./src/screens/ProjectDetailsScreen";
import ApprovalInboxScreen from "./src/screens/ApprovalInboxScreen";
import ReleaseReviewScreen from "./src/screens/ReleaseReviewScreen";
import MonitoringScreen from "./src/screens/MonitoringScreen";
import NotificationsScreen from "./src/screens/NotificationsScreen";
import SettingsScreen from "./src/screens/SettingsScreen";
import { colors } from "./src/theme";
import { Project } from "./src/types";

type Tab = "dashboard" | "projects" | "approvals" | "monitoring" | "activity" | "settings";
type ViewState = { kind: "tab"; tab: Tab } | { kind: "project"; project: Project } | { kind: "review"; approvalId: number };
const tabs: { id: Tab; title: string; glyph: string }[] = [
  { id: "dashboard", title: "Home", glyph: "⌂" },
  { id: "projects", title: "Projects", glyph: "▦" },
  { id: "approvals", title: "Inbox", glyph: "✓" },
  { id: "monitoring", title: "Monitor", glyph: "⌁" },
  { id: "activity", title: "Activity", glyph: "◷" },
];

export default function App() {
  const [view, setView] = useState<ViewState>({ kind: "tab", tab: "dashboard" });
  const [origin, setOrigin] = useState<Tab>("dashboard");
  const portfolio = useRemote(api.projects);
  const isTab = view.kind === "tab";
  const activeTab = isTab ? view.tab : origin;

  const navigate = (tab: Tab) => setView({ kind: "tab", tab });
  const openProject = (project: Project, from: Tab = activeTab) => { setOrigin(from); setView({ kind: "project", project }); };
  const back = () => setView({ kind: "tab", tab: view.kind === "tab" ? activeTab : origin });
  const title = view.kind === "project" ? "Project details" : view.kind === "review" ? "Release review" : tabs.find((item) => item.id === view.tab)?.title || "CodeDeck";

  return <SafeAreaView style={styles.safe}>
    <StatusBar barStyle="dark-content" backgroundColor={colors.canvas} />
    <View style={styles.app}>
      <View style={styles.header}>
        {view.kind !== "tab" ? <Pressable onPress={back} accessibilityRole="button" style={styles.headerBack}><Text style={styles.headerBackText}>‹</Text></Pressable> : null}
        <View style={styles.brandMark}><Text style={styles.brandGlyph}>C</Text></View>
        <View style={styles.brandText}><Text style={styles.brandName}>CodeDeck</Text><Text style={styles.brandContext}>MANAGER · {title.toUpperCase()}</Text></View>
        {view.kind === "tab" && view.tab !== "settings" ? <Pressable accessibilityRole="button" onPress={() => navigate("settings")} style={styles.settingsButton}><Text style={styles.settingsGlyph}>⚙</Text></Pressable> : null}
      </View>
      <View style={styles.main}>
        {view.kind === "tab" && view.tab === "dashboard" ? <DashboardScreen onProject={(project) => openProject(project, "dashboard")} onApproval={(id) => setView({ kind: "review", approvalId: id })} onApprovals={() => navigate("approvals")} /> : null}
        {view.kind === "tab" && view.tab === "projects" ? <ProjectsScreen onProject={(project) => openProject(project, "projects")} /> : null}
        {view.kind === "tab" && view.tab === "approvals" ? <ApprovalInboxScreen onReview={(id) => setView({ kind: "review", approvalId: id })} /> : null}
        {view.kind === "tab" && view.tab === "monitoring" ? <MonitoringScreen projects={portfolio.data || []} /> : null}
        {view.kind === "tab" && view.tab === "activity" ? <NotificationsScreen /> : null}
        {view.kind === "tab" && view.tab === "settings" ? <SettingsScreen /> : null}
        {view.kind === "project" ? <ProjectDetailsScreen project={view.project} /> : null}
        {view.kind === "review" ? <ReleaseReviewScreen approvalId={view.approvalId} /> : null}
      </View>
      {isTab ? <View style={styles.tabBar}>{tabs.map((tab) => <Pressable key={tab.id} accessibilityRole="tab" accessibilityState={{ selected: activeTab === tab.id }} onPress={() => navigate(tab.id)} style={styles.tab}>
        <Text style={[styles.tabGlyph, activeTab === tab.id && styles.tabSelected]}>{tab.glyph}</Text><Text style={[styles.tabLabel, activeTab === tab.id && styles.tabSelected]}>{tab.title}</Text>
      </Pressable>)}</View> : null}
    </View>
  </SafeAreaView>;
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: colors.canvas },
  app: { flex: 1, backgroundColor: colors.canvas },
  header: { minHeight: 60, paddingHorizontal: 16, flexDirection: "row", alignItems: "center", gap: 10, borderBottomWidth: 1, borderBottomColor: colors.border, backgroundColor: colors.surface },
  headerBack: { width: 30, height: 40, justifyContent: "center" }, headerBackText: { color: colors.blue, fontSize: 30, lineHeight: 34 },
  brandMark: { width: 32, height: 32, borderRadius: 8, backgroundColor: colors.blue, alignItems: "center", justifyContent: "center" }, brandGlyph: { color: "#FFFFFF", fontWeight: "800", fontSize: 18 },
  brandText: { flex: 1 }, brandName: { color: colors.ink, fontSize: 15, fontWeight: "800", letterSpacing: 0.2 }, brandContext: { color: colors.secondary, fontSize: 9, fontWeight: "700", letterSpacing: 1, marginTop: 2 },
  settingsButton: { width: 40, height: 42, justifyContent: "center", alignItems: "center" }, settingsGlyph: { color: colors.secondary, fontSize: 19 }, main: { flex: 1 },
  tabBar: { flexDirection: "row", borderTopWidth: 1, borderTopColor: colors.border, backgroundColor: colors.surface, paddingTop: 7, paddingBottom: 8 },
  tab: { flex: 1, alignItems: "center", justifyContent: "center", minHeight: 48, gap: 2 }, tabGlyph: { color: colors.secondary, fontSize: 18, fontWeight: "700" }, tabLabel: { color: colors.secondary, fontSize: 10, fontWeight: "600" }, tabSelected: { color: colors.blue },
});
