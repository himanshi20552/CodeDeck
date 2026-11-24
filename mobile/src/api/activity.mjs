const asObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value) ? value : null;
const asList = (value) => Array.isArray(value) ? value : [];
const asText = (value) => typeof value === "string" && value.trim() ? value.trim() : null;

/** Converts monitoring and approval API payloads to short, render-safe activity items. */
export function normalizeActivity(monitoringValue, approvalsValue) {
  const monitoring = asObject(monitoringValue) || {};
  const approvals = asObject(approvalsValue) || {};
  const items = [];

  for (const value of asList(monitoring.alerts)) {
    const event = asObject(value);
    if (!event) continue;
    const id = event.id ?? event.fingerprint;
    items.push({
      key: `alert-${String(id ?? items.length)}`,
      kind: "Alert",
      title: asText(event.title) || asText(event.type) || "CodeDeck alert",
      detail: asText(event.message) || "Alert details were not recorded.",
      time: asText(event.last_seen) || asText(event.first_seen),
      status: asText(event.severity) || "Recorded",
    });
  }

  for (const value of asList(monitoring.deployment_events)) {
    const event = asObject(value);
    if (!event) continue;
    const status = asText(event.status) || "Status unavailable";
    const logs = asList(event.logs).map(asObject).filter(Boolean);
    const lastLog = logs.length ? logs[logs.length - 1] : null;
    const deployment = asText(event.deployment_type);
    items.push({
      key: `deployment-${String(event.id ?? items.length)}`,
      kind: "Deployment",
      title: [asText(event.environment), asText(event.version), status].filter(Boolean).join(" · ") || "Deployment update",
      detail: asText(lastLog?.message) || deployment || "Deployment details were not recorded.",
      time: asText(event.created_at),
      status,
    });
  }

  for (const value of asList(monitoring.file_activity)) {
    const event = asObject(value);
    if (!event) continue;
    const eventType = asText(event.event_type) || "File change";
    const path = asText(event.path);
    items.push({
      key: `file-${String(event.id ?? items.length)}`,
      kind: "Repository",
      title: path ? `${eventType} · ${path}` : eventType,
      detail: asText(event.summary) || asText(event.workflow_context) || "Repository activity recorded.",
      time: asText(event.occurred_at),
      status: asText(event.processing_status) || "Recorded",
    });
  }

  for (const value of asList(approvals.items)) {
    const approval = asObject(value);
    if (!approval) continue;
    const project = asObject(approval.project);
    const events = asList(approval.events);
    for (const value of events) {
      const event = asObject(value);
      if (!event) continue;
      const release = asText(approval.release_version) || asText(approval.commit_id) || approval.id;
      const projectName = asText(project?.name) || `Project ${String(approval.project_id ?? "")}`.trim();
      const state = asText(event.to_status) || "Release update";
      items.push({
        key: `approval-${String(event.id ?? `${approval.id}-${items.length}`)}`,
        kind: "Release",
        title: `${projectName} · ${state}`,
        detail: asText(event.comments) || asText(approval.commit_message) || `Release ${String(release ?? "details unavailable")}`,
        time: asText(event.occurred_at),
        status: state,
      });
    }
  }

  const timeValue = (value) => {
    if (!value) return Number.NEGATIVE_INFINITY;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
  };
  return items.sort((left, right) => timeValue(right.time) - timeValue(left.time));
}
