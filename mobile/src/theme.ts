import { Platform } from "react-native";

export const colors = {
  canvas: "#F4F6F8",
  surface: "#FFFFFF",
  surfaceMuted: "#F8FAFC",
  ink: "#172B4D",
  secondary: "#64748B",
  muted: "#94A3B8",
  border: "#E2E8F0",
  blue: "#2457A7",
  blueSoft: "#EAF1FB",
  teal: "#117A72",
  tealSoft: "#E5F5F2",
  amber: "#9A6700",
  amberSoft: "#FFF4D6",
  red: "#B42318",
  redSoft: "#FDECEA",
  slateSoft: "#EEF2F6",
};

export const shadow = Platform.select({
  ios: { shadowColor: "#172B4D", shadowOpacity: 0.04, shadowRadius: 8, shadowOffset: { width: 0, height: 3 } },
  android: { elevation: 1 },
  default: {},
});
