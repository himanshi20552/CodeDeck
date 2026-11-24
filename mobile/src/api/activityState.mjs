export function activityViewState({ loading, error, entries }) {
  if (loading) return "loading";
  if (error) return "error";
  return entries.length ? "content" : "empty";
}
