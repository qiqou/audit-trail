export interface ApiErrorBody {
  detail?: string;
}

/**
 * 本地 HTTP 通道的唯一实现：会话头、网络故障、会话失效和二进制下载都在此收口。
 * 业务 API 不得自行猜测错误文本或重复拼接 X-Session。
 */
export class HttpClient {
  private token = sessionStorage.getItem("audit_token") ?? "";

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (this.token) headers.set("X-Session", this.token);
    if (init.body && !(init.body instanceof FormData) && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    let response: Response;
    try {
      response = await fetch(path, { ...init, headers });
    } catch {
      throw new Error("无法连接本地服务，请确认审迹已启动");
    }
    const body = (await response.json().catch(() => null)) as T | ApiErrorBody | null;
    if (!response.ok) throw this.responseError(body as ApiErrorBody | null, response.status);
    return body as T;
  }

  setSession(token: string, operator: string): void {
    this.token = token;
    sessionStorage.setItem("audit_token", token);
    sessionStorage.setItem("audit_operator", operator);
  }

  clearSession(): void {
    this.token = "";
    sessionStorage.removeItem("audit_token");
    sessionStorage.removeItem("audit_operator");
  }

  async downloadUrl(path: string, filename: string): Promise<void> {
    const headers = new Headers();
    if (this.token) headers.set("X-Session", this.token);
    let response: Response;
    try {
      response = await fetch(path, { headers });
    } catch {
      throw new Error("无法连接本地服务，请确认审迹已启动");
    }
    if (!response.ok) {
      const body = (await response.json().catch(() => null)) as ApiErrorBody | null;
      throw this.responseError(body, response.status, "下载失败");
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }

  private responseError(body: ApiErrorBody | null, status: number, action = "请求失败"): Error {
    const message = body?.detail ?? `${action}（${status}）`;
    if (message.includes("使用人会话无效")) {
      this.clearSession();
      window.dispatchEvent(new CustomEvent("audit-session-expired"));
    }
    return new Error(message);
  }
}
