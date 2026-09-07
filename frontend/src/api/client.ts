import { message } from 'antd';

// 统一 fetch 封装：base = /api，错误 → antd message.error，含中文释义。
// 注：使用静态 message API（antD 5）。main.tsx 已用 App 包裹，但 client 在 React 树外，
// 此处仅用于错误提示，主题差异可忽略。
const TOKEN_KEY = 'bv_api_token';

declare global {
  interface Window { __BV_TOKEN__?: string }
}

// 构建期注入（VITE_BV_API_TOKEN，前端注入模式）：打开网页自动携带，零手动配置。
// 手动粘贴（Settings 页，localStorage）仍作为覆盖项。
const BUILTIN_TOKEN =
  (typeof import.meta !== 'undefined' && (import.meta.env?.VITE_BV_API_TOKEN as string | undefined)) ||
  (typeof window !== 'undefined' ? window.__BV_TOKEN__ || '' : '') ||
  '';

/** P0-1 鉴权：令牌来源 = 构建内置 > 手动配置(localStorage)。未配置时后端 fail-closed 401。 */
export function getApiToken(): string {
  return BUILTIN_TOKEN || localStorage.getItem(TOKEN_KEY) || '';
}
export function tokenSource(): 'builtin' | 'manual' | 'none' {
  if (BUILTIN_TOKEN) return 'builtin';
  return localStorage.getItem(TOKEN_KEY) ? 'manual' : 'none';
}
export function setApiToken(t: string) {
  if (t) localStorage.setItem(TOKEN_KEY, t.trim());
  else localStorage.removeItem(TOKEN_KEY);
}

export function setAntdApp(_app: unknown) {
  /* 保留入口，静态 message 无需注入 */
}

function msg() {
  return message;
}

export class ApiError extends Error {
  code: string | number;
  constructor(code: string | number, message: string) {
    super(message);
    this.code = code;
    this.name = 'ApiError';
  }
}

const OKX_CODE_CN: Record<string, string> = {
  '51008': '订单失败：账户余额不足或下单数量过小',
  '51004': '订单失败：参数错误',
  '51009': '订单失败：订单价格超过限制',
  '51121': '订单失败：数量低于最小下单量',
  '51100': '订单失败：交易对参数错误',
  '51400': '订单状态：订单已成交或已撤销',
  '51401': '订单状态：订单不存在',
  '50011': '请求过于频繁（交易所限频）',
  '50013': '请求繁忙，请稍后重试',
};

export function explainOkxCode(code: string | number): string {
  return OKX_CODE_CN[String(code)] || '';
}

async function request<T>(method: string, path: string, opts?: {
  query?: Record<string, any>;
  body?: any;
}): Promise<T> {
  const { query, body } = opts || {};
  let url = '/api' + path;
  if (query && Object.keys(query).length) {
    const usp = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== '') usp.append(k, String(v));
    }
    url += '?' + usp.toString();
  }
  const init: RequestInit = { method, headers: {} as Record<string, string> };
  // P0-1：统一携带 Bearer Token（未配置由后端 401 提示）
  const token = getApiToken();
  if (token) (init.headers as Record<string, string>)['Authorization'] = 'Bearer ' + token;
  // 高风险写操作需要 confirm=true（后端 middleware 校验；前端已有人工确认弹窗）
  if (method !== 'GET' && body !== undefined && typeof body === 'object' && !Array.isArray(body)) {
    init.headers = { 'Content-Type': 'application/json', ...init.headers };
    init.body = JSON.stringify({ confirm: true, ...body });
  } else if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json', ...init.headers };
    init.body = JSON.stringify(body);
  }
  try {
    const resp = await fetch(url, init);
    const text = await resp.text();
    let data: any = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      throw new ApiError(resp.status, `响应解析失败: ${text.slice(0, 200)}`);
    }
    if (!resp.ok) {
      // FastAPI HTTPException 抛出 {"detail": "..."}；后端 OMS 把交易所错误包成 RiskBlocked→400/403
      const detail =
        (data && (data.detail ?? data.error ?? data.msg)) ||
        `HTTP ${resp.status}`;
      // 若 detail 形如 "交易所拒绝下单 [code] msg"，提取 code 给中文释义
      const m = /\[(\w+)\]/.exec(String(detail));
      if (m) {
        const cn = explainOkxCode(m[1]);
        msg()?.error(`${detail}${cn ? '（' + cn + '）' : ''}`);
      } else {
        msg()?.error(String(detail));
      }
      throw new ApiError(resp.status, String(detail));
    }
    return data as T;
  } catch (e) {
    if (e instanceof ApiError) throw e;
    msg()?.error('网络请求失败：' + (e as Error).message);
    throw new ApiError(-1, (e as Error).message);
  }
}

export const api = {
  get: <T>(path: string, query?: Record<string, any>) =>
    request<T>('GET', path, { query }),
  post: <T>(path: string, body?: any, query?: Record<string, any>) =>
    request<T>('POST', path, { body, query }),
  del: <T>(path: string, query?: Record<string, any>) =>
    request<T>('DELETE', path, { query }),
};
