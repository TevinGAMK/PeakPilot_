export class ApiError extends Error {
  status: number;
  code: string;
  detail: any;
  constructor(status: number, code: string, message: string, detail?: any) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown, auth?: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: { ...(body ? { "content-type": "application/json" } : {}), ...(auth ? { authorization: `Bearer ${auth}` } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "network", "Can't reach the PerkPilot API. Is the backend running on port 8000?");
  }
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const err = data?.error;
    if (err) throw new ApiError(res.status, err.code, err.message, err.detail);
    const msg = Array.isArray(data?.detail)
      ? data.detail.map((d: any) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ")
      : `Request failed (${res.status})`;
    throw new ApiError(res.status, "invalid_request", msg, data);
  }
  return data as T;
}

// The admin audit token lives only for this browser tab; the server checks it on every /api/admin request.
const ADMIN_KEY = "perkpilot_admin_token";
let memoToken = ""; // fallback when storage is blocked: lasts until reload
export const adminToken = {
  get: (): string => {
    try { return sessionStorage.getItem(ADMIN_KEY) ?? memoToken; } catch { return memoToken; }
  },
  set: (t: string) => {
    memoToken = t;
    try { if (t) sessionStorage.setItem(ADMIN_KEY, t); else sessionStorage.removeItem(ADMIN_KEY); } catch { /* memoToken covers it */ }
  },
};

export const api = {
  get: <T = any>(path: string) => request<T>("GET", path),
  admin: <T = any>(path: string) => request<T>("GET", path, undefined, adminToken.get()),
  adminPost: <T = any>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}, adminToken.get()),
  post: <T = any>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
};
