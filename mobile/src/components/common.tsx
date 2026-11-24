import React from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View, ViewStyle } from "react-native";
import { colors, shadow } from "../theme";

export function PageTitle({ title, subtitle }: { title: string; subtitle?: string }) {
  return <View style={styles.titleWrap}><Text style={styles.title}>{title}</Text>{subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}</View>;
}

export function Panel({ children, style }: { children: React.ReactNode; style?: ViewStyle }) {
  return <View style={[styles.panel, style]}>{children}</View>;
}

export function Label({ children, style }: { children: React.ReactNode; style?: any }) {
  return <Text style={[styles.label, style]}>{children}</Text>;
}

export function StatusPill({ status }: { status?: string | null }) {
  const normalized = (status || "Unknown").toLowerCase();
  const palette = normalized.includes("fail") || normalized.includes("reject") || normalized.includes("crash")
    ? [colors.redSoft, colors.red]
    : normalized.includes("complete") || normalized.includes("healthy") || normalized.includes("approved") || normalized.includes("succeed")
      ? [colors.tealSoft, colors.teal]
      : normalized.includes("block") || normalized.includes("pending") || normalized.includes("queued") || normalized.includes("await")
        ? [colors.amberSoft, colors.amber] : [colors.slateSoft, colors.secondary];
  return <View style={[styles.pill, { backgroundColor: palette[0] }]}><Text style={[styles.pillText, { color: palette[1] }]}>{status || "Unknown"}</Text></View>;
}

export function ActionButton({ title, onPress, variant = "primary", disabled = false }: {
  title: string; onPress: () => void; variant?: "primary" | "secondary" | "danger"; disabled?: boolean;
}) {
  const bg = variant === "danger" ? colors.red : variant === "secondary" ? colors.surface : colors.blue;
  const fg = variant === "secondary" ? colors.ink : "#FFFFFF";
  return <Pressable accessibilityRole="button" disabled={disabled} onPress={onPress} style={({ pressed }) => [styles.button, { backgroundColor: bg, borderColor: variant === "secondary" ? colors.border : bg, opacity: disabled ? 0.45 : pressed ? 0.82 : 1 }]}><Text style={[styles.buttonText, { color: fg }]}>{title}</Text></Pressable>;
}

export function Field({ value, onChangeText, placeholder, multiline = false }: {
  value: string; onChangeText: (value: string) => void; placeholder: string; multiline?: boolean;
}) {
  return <TextInput value={value} onChangeText={onChangeText} placeholder={placeholder} placeholderTextColor={colors.muted}
    multiline={multiline} textAlignVertical={multiline ? "top" : "center"}
    style={[styles.field, multiline && styles.multiline]} autoCapitalize="none" />;
}

export function LoadingState({ label = "Loading CodeDeck data…" }: { label?: string }) {
  return <View style={styles.state}><ActivityIndicator color={colors.blue} /><Text style={styles.stateText}>{label}</Text></View>;
}

export function MessageState({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <Panel style={styles.message}><Text style={styles.messageTitle}>{title}</Text>{detail ? <Text style={styles.messageBody}>{detail}</Text> : null}{action}</Panel>;
}

export function formatDate(value?: string | null) {
  if (!value) return "Not recorded";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Not recorded" : date.toLocaleString();
}

export function formatDuration(seconds?: number | null) {
  if (seconds == null || !Number.isFinite(seconds)) return "Not available";
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))} sec`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} sec`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours} hr ${minutes} min`;
}

const styles = StyleSheet.create({
  titleWrap: { marginBottom: 18, gap: 4 }, title: { color: colors.ink, fontSize: 25, fontWeight: "700", letterSpacing: -0.5 },
  subtitle: { color: colors.secondary, fontSize: 14, lineHeight: 20 }, panel: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.border, borderRadius: 12, padding: 16, ...shadow },
  label: { color: colors.secondary, fontSize: 12, fontWeight: "600", letterSpacing: 0.3, textTransform: "uppercase" },
  pill: { alignSelf: "flex-start", borderRadius: 6, paddingHorizontal: 9, paddingVertical: 5 }, pillText: { fontSize: 11, fontWeight: "700" },
  button: { minHeight: 46, paddingHorizontal: 16, borderWidth: 1, borderRadius: 9, alignItems: "center", justifyContent: "center" },
  buttonText: { fontSize: 14, fontWeight: "700" }, field: { minHeight: 46, paddingHorizontal: 12, borderRadius: 9, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.surface, color: colors.ink, fontSize: 15 }, multiline: { minHeight: 96, paddingTop: 12 },
  state: { minHeight: 150, justifyContent: "center", alignItems: "center", gap: 12 }, stateText: { color: colors.secondary, fontSize: 14 },
  message: { gap: 8 }, messageTitle: { color: colors.ink, fontSize: 16, fontWeight: "700" }, messageBody: { color: colors.secondary, fontSize: 14, lineHeight: 21 },
});
