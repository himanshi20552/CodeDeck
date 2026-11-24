import React, { useEffect, useRef, useState } from "react";
import { Alert, AppState, Pressable, SafeAreaView, StatusBar, StyleSheet, Text, View } from "react-native";
import { api } from "./src/api/client";
import { findNewApprovalRequests } from "./src/api/approvalNotifications.mjs";
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
import { Approval, Project } from "./src/types";

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
  const [pendingApprovals, setPendingApprovals] = useState<Approval[]>([]);
  const [approvalMode, setApprovalMode] = useState<"demo" | "token" | undefined>();
  const [approvalPollError, setApprovalPollError] = useState<string | null>(null);
  const knownApprovalIds = useRef<number[] | null>(null);
  const polling = useRef(false);
  const portfolio = useRemote(api.projects);
  const isTab = view.kind === "tab";
  const activeTab = isTab ? view.tab : origin;

  const navigate = (tab: Tab) => setView({ kind: "tab", tab });
  const openProject = (project: Project, from: Tab = activeTab) => { setOrigin(from); setView({ kind: "project", project }); };
  const back = () => setView({ kind: "tab", tab: view.kind === "tab" ? activeTab : origin });
  const title = view.kind === "project" ? "Project details" : view.kind === "review" ? "Release review" : tabs.find((item) => item.id === view.tab)?.title || "CodeDeck";

  useEffect(() => {
    let active = true;
    const refreshApprovals = async () => {
      if (polling.current) return;
      polling.current = true;
      try {
        const response = await api.pendingApprovals();
        if (!active) return;
        const items = Array.isArray(response.items) ? response.items : [];
        const newlyPending = findNewApprovalRequests(knownApprovalIds.current, items);
        knownApprovalIds.current = items.map((item) => item.id);
        setPendingApprovals(items);
        setApprovalMode(response.mode);
        setApprovalPollError(null);
        if (newlyPending.length) {
          const latest = newlyPending[newlyPending.length - 1];
          const name = latest.project?.name || `Project ${latest.project_id}`;
          Alert.alert("Release awaiting approval", `${name} has a new request from CodeDeck.`, [
            { text: "Later", style: "cancel" },
            { text: "Review", onPress: () => setView({ kind: "review", approvalId: latest.id }) },
          ]);
        }
      } catch (error) {
        // The inbox shows the backend's real auth/network error. Avoid repeated popups here.
        if (active) setApprovalPollError(error instanceof Error ? error.message : "Approval inbox unavailable.");
      } finally {
        polling.current = false;
      }
    };
    void refreshApprovals();
    const timer = setInterval(() => { if (AppState.currentState === "active") void refreshApprovals(); }, 30_000);
    const subscription = AppState.addEventListener("change", (state) => { if (state === "active") void refreshApprovals(); });
    return () => { active = false; clearInterval(timer); subscription.remove(); };
  }, []);

  return <SafeAreaView style={styles.safe}>
    <StatusBar barStyle="dark-content" />
    <View style={styles.app}>
      <View style={styles.header}>
        {view.kind !== "tab" ? <Pressable onPress={back} accessibilityRole="button" style={styles.headerBack}><Text style={styles.headerBackText}>‹</Text></Pressable> : null}
        <View style={styles.brandMark}><Text style={styles.brandGlyph}>C</Text></View>
        <View style={styles.brandText}><Text style={styles.brandName}>CodeDeck</Text><Text style={styles.brandContext}>MANAGER · {title.toUpperCase()}</Text></View>
        {view.kind === "tab" && view.tab !== "settings" ? <Pressable accessibilityRole="button" onPress={() => navigate("settings")} style={styles.settingsButton}><Text style={styles.settingsGlyph}>⚙</Text></Pressable> : null}
      </View>
      <View style={styles.main}>
        {pendingApprovals.length > 0 ? <Pressable accessibilityRole="button" onPress={() => navigate("approvals")} style={styles.approvalNotice}><View style={{ flex: 1 }}><Text style={styles.approvalNoticeTitle}>{pendingApprovals.length} release{pendingApprovals.length === 1 ? "" : "s"} awaiting review</Text><Text style={styles.approvalNoticeText}>{approvalMode === "demo" ? "Local demo · actions are unverified and do not deploy" : "Open the inbox to review release details"}</Text></View><Text style={styles.approvalNoticeAction}>Review ›</Text></Pressable> : null}
        {view.kind === "tab" && view.tab === "dashboard" ? <DashboardScreen onProject={(project) => openProject(project, "dashboard")} onApproval={(id) => setView({ kind: "review", approvalId: id })} onApprovals={() => navigate("approvals")} pending={pendingApprovals} approvalError={approvalPollError} approvalMode={approvalMode} /> : null}
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
  approvalNotice: { minHeight: 56, marginHorizontal: 12, marginTop: 8, paddingHorizontal: 12, paddingVertical: 9, flexDirection: "row", alignItems: "center", gap: 10, borderRadius: 9, borderWidth: 1, borderColor: "#F1D99A", backgroundColor: colors.amberSoft }, approvalNoticeTitle: { color: colors.amber, fontWeight: "700", fontSize: 13 }, approvalNoticeText: { color: colors.secondary, fontSize: 11, marginTop: 2 }, approvalNoticeAction: { color: colors.blue, fontWeight: "700", fontSize: 12 },
  tabBar: { flexDirection: "row", borderTopWidth: 1, borderTopColor: colors.border, backgroundColor: colors.surface, paddingTop: 7, paddingBottom: 8 },
  tab: { flex: 1, alignItems: "center", justifyContent: "center", minHeight: 48, gap: 2 }, tabGlyph: { color: colors.secondary, fontSize: 18, fontWeight: "700" }, tabLabel: { color: colors.secondary, fontSize: 10, fontWeight: "600" }, tabSelected: { color: colors.blue },
});
