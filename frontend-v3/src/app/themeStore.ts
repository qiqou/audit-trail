import { defineStore } from "pinia";

export type Theme = "dark" | "light" | "green" | "paper";

function initialTheme(): Theme {
  const stored = localStorage.getItem("audit_theme");
  return stored === "light" || stored === "green" || stored === "dark" || stored === "paper"
    ? stored
    : "dark";
}

/** 纯界面偏好；不包含项目或底稿业务数据。 */
export const useThemeStore = defineStore("audit-theme", {
  state: () => ({ theme: initialTheme() }),
  actions: {
    apply(theme: Theme): void {
      this.theme = theme;
      document.documentElement.dataset.theme = theme;
      document.documentElement.classList.toggle("dark", theme === "dark");
      localStorage.setItem("audit_theme", theme);
    },
  },
});
