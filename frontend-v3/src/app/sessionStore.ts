import { defineStore } from "pinia";

/** 仅保存本地服务会话的展示身份；权限和项目连接仍由后端判定。 */
export const useSessionStore = defineStore("audit-session", {
  state: () => ({ operator: sessionStorage.getItem("audit_operator") ?? "" }),
  actions: {
    setOperator(operator: string): void {
      this.operator = operator;
    },
    clearOperator(): void {
      this.operator = "";
    },
  },
});
