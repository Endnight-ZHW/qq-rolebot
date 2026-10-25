import { reactive } from "vue";
import type { Api } from "./types";
export const session = reactive({
  authenticated: false,
  ready: false,
  csrf: "",
});
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public fields: Record<string, string> = {},
  ) {
    super(message);
  }
}
export const api: Api = async (path, method = "GET", body?, signal?) => {
  const headers: Record<string, string> = {};
  const options: RequestInit = {
    method,
    credentials: "same-origin",
    headers,
    signal,
  };
  if (method !== "GET") {
    headers["X-CSRF-Token"] = session.csrf;
    if (body instanceof FormData) options.body = body;
    else {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body ?? {});
    }
  }
  let response: Response;
  try {
    response = await fetch("/api/v1" + path, options);
  } catch (e: any) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("暂时无法连接服务，请检查网络后重试。", 0);
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && path !== "/auth/login") {
      session.authenticated = false;
      session.csrf = "";
    }
    const fields: Record<string, string> = {};
    if (Array.isArray(data.detail))
      for (const e of data.detail)
        fields[e.loc?.slice(1).join(".") || "form"] = e.msg;
    if (Array.isArray(data.fields))
      for (const location of data.fields)
        fields[location.slice(1).join(".") || "form"] =
          typeof data.detail === "string" ? data.detail : "请检查该字段";
    throw new ApiError(
      typeof data.detail === "string"
        ? data.detail
        : Object.values(fields).join("；") || "请求失败，请稍后重试。",
      response.status,
      fields,
    );
  }
  return data;
};
export function query(values: Record<string, unknown>) {
  return new URLSearchParams(
    Object.entries(values)
      .filter(([, v]) => v !== "" && v !== undefined && v !== null)
      .map(([k, v]) => [k, String(v)]),
  ).toString();
}
