export type ErrorActionKind =
  | "login"
  | "retry"
  | "wait"
  | "quota"
  | "archive"
  | "input"
  | "back"
  | "none";

export interface ErrorAction {
  kind: ErrorActionKind;
  label: string;
}

const ACTIONS: Record<string, ErrorAction> = {
  TOKEN_EXPIRED: { kind: "login", label: "重新登录" },
  UNAUTHENTICATED: { kind: "login", label: "重新登录" },
  INVALID_REFRESH_TOKEN: { kind: "login", label: "重新登录" },
  INVALID_CREDENTIALS: { kind: "input", label: "检查邮箱/密码" },
  USER_DISABLED: { kind: "none", label: "联系管理员" },
  RATE_LIMITED: { kind: "wait", label: "按倒计时等待" },
  QUOTA_EXCEEDED: { kind: "quota", label: "查看配额" },
  CONVERSATION_ARCHIVED: { kind: "archive", label: "取消归档" },
  AI_UNAVAILABLE: { kind: "retry", label: "稍后重试" },
  AI_OVERLOADED: { kind: "retry", label: "稍后重试" },
  AI_TIMEOUT: { kind: "retry", label: "稍后重试" },
  DEPENDENCY_UNAVAILABLE: { kind: "retry", label: "稍后重试" },
  SERVICE_SHUTTING_DOWN: { kind: "retry", label: "稍后重试" },
  INTERNAL_ERROR: { kind: "retry", label: "稍后重试" },
  SUMMARY_UNAVAILABLE: { kind: "none", label: "可重建摘要" },
  INVALID_ARGUMENT: { kind: "input", label: "检查输入" },
  PAYLOAD_TOO_LARGE: { kind: "input", label: "缩小文件" },
  UNSUPPORTED_MEDIA_TYPE: { kind: "input", label: "更换文件类型" },
  EMAIL_ALREADY_EXISTS: { kind: "input", label: "换邮箱或登录" },
  CONVERSATION_NOT_FOUND: { kind: "back", label: "返回工作台" },
  MESSAGE_NOT_FOUND: { kind: "back", label: "刷新列表" },
  RESOURCE_NOT_FOUND: { kind: "back", label: "返回列表" },
};

export function errorAction(code: string | undefined): ErrorAction {
  if (!code) return { kind: "none", label: "无自动动作" };
  return ACTIONS[code] ?? { kind: "none", label: "无自动动作" };
}
