import { API_BASE } from "@/shared/api/base";
import { apiRequest } from "@/shared/api/client";
import { ApiError, isApiError, toApiError } from "@/shared/api/error";
import { getAccessToken, refreshOnce } from "@/shared/api/session";

import type {
  DocumentChunk,
  DocumentFilters,
  KnowledgeBase,
  KnowledgeBaseFilters,
  KnowledgeBaseInput,
  KnowledgeDocument,
  SearchRequest,
  SearchResponse,
  UploadAccepted,
} from "./types";

function pageQuery(filters: { limit?: number; cursor?: string | null; status?: string }) {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 20));
  if (filters.cursor) params.set("cursor", filters.cursor);
  if (filters.status) params.set("status", filters.status);
  return params.toString();
}

export function listKnowledgeBases(
  filters: KnowledgeBaseFilters = {},
): Promise<{ items: KnowledgeBase[]; next_cursor: string | null; has_more: boolean }> {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 50));
  if (filters.cursor) params.set("cursor", filters.cursor);
  return apiRequest(`/knowledge-bases?${params.toString()}`);
}

export function createKnowledgeBase(input: KnowledgeBaseInput): Promise<KnowledgeBase> {
  return apiRequest<KnowledgeBase>("/knowledge-bases", { method: "POST", json: input });
}

export function updateKnowledgeBase(
  id: string,
  patch: Partial<KnowledgeBaseInput>,
): Promise<KnowledgeBase> {
  return apiRequest<KnowledgeBase>(`/knowledge-bases/${encodeURIComponent(id)}`, {
    method: "PATCH",
    json: patch,
  });
}

export function deleteKnowledgeBase(
  id: string,
  force = true,
): Promise<{ deleted: boolean; documents: number; vectors: number; objects: number }> {
  return apiRequest(`/knowledge-bases/${encodeURIComponent(id)}?force=${force}`, {
    method: "DELETE",
  });
}

export function listDocuments(
  kbId: string,
  filters: DocumentFilters = {},
): Promise<{ items: KnowledgeDocument[]; next_cursor: string | null; has_more: boolean }> {
  return apiRequest(
    `/knowledge-bases/${encodeURIComponent(kbId)}/documents?${pageQuery(filters)}`,
  );
}

export function getDocument(docId: string): Promise<KnowledgeDocument> {
  return apiRequest(`/documents/${encodeURIComponent(docId)}`);
}

export function listChunks(
  docId: string,
  filters: { limit?: number; cursor?: string | null } = {},
): Promise<{ items: DocumentChunk[]; next_cursor: string | null; has_more: boolean }> {
  const params = new URLSearchParams();
  params.set("limit", String(filters.limit ?? 30));
  if (filters.cursor) params.set("cursor", filters.cursor);
  return apiRequest(`/documents/${encodeURIComponent(docId)}/chunks?${params.toString()}`);
}

export function deleteDocument(docId: string): Promise<{ doc_id: string; task_id: string; status: string }> {
  return apiRequest(`/documents/${encodeURIComponent(docId)}`, { method: "DELETE" });
}

export function searchKnowledgeBase(
  kbId: string,
  request: SearchRequest,
): Promise<SearchResponse> {
  return apiRequest(`/knowledge-bases/${encodeURIComponent(kbId)}/search`, {
    method: "POST",
    json: request,
  });
}

export interface DocumentDownloadInfo {
  url: string | null;
  expires_in: number;
  doc_name: string;
  mime_type: string;
}

export function getDocumentDownloadInfo(docId: string): Promise<DocumentDownloadInfo> {
  return apiRequest(`/documents/${encodeURIComponent(docId)}/download-url`);
}

export async function downloadDocumentBlob(docId: string): Promise<Blob> {
  const headers = new Headers();
  const token = getAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let response = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}/download`, {
    headers,
    credentials: "include",
  });
  if (response.status === 401) {
    const error = await toApiError(response);
    if (error.code === "TOKEN_EXPIRED" && (await refreshOnce())) {
      const refreshed = new Headers();
      const nextToken = getAccessToken();
      if (nextToken) refreshed.set("Authorization", `Bearer ${nextToken}`);
      response = await fetch(`${API_BASE}/documents/${encodeURIComponent(docId)}/download`, {
        headers: refreshed,
        credentials: "include",
      });
    } else {
      throw error;
    }
  }
  if (!response.ok) throw await toApiError(response);
  return response.blob();
}

export interface UploadOptions {
  onProgress?: (percent: number, loaded: number, total: number) => void;
  signal?: AbortSignal;
}

function sendUpload(
  kbId: string,
  file: File,
  options: UploadOptions,
): Promise<UploadAccepted> {
  return new Promise((resolve, reject) => {
    if (options.signal?.aborted) {
      reject(new DOMException("上传已取消", "AbortError"));
      return;
    }

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/knowledge-bases/${encodeURIComponent(kbId)}/documents`);
    xhr.responseType = "text";
    xhr.timeout = 180_000;
    xhr.withCredentials = true;
    xhr.setRequestHeader("X-Request-Id", `web_${crypto.randomUUID()}`);
    xhr.setRequestHeader("Idempotency-Key", crypto.randomUUID());

    const token = getAccessToken();
    if (token) xhr.setRequestHeader("Authorization", `Bearer ${token}`);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        options.onProgress?.(
          Math.round((event.loaded / event.total) * 100),
          event.loaded,
          event.total,
        );
      }
    };

    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch {
        body = null;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as UploadAccepted);
        return;
      }

      const envelope = body as {
        error?: { code?: unknown; message?: unknown; details?: unknown; trace_id?: unknown; retryable?: unknown };
      } | null;
      const error = envelope?.error;
      reject(
        new ApiError({
          code: typeof error?.code === "string" ? error.code : `HTTP_${xhr.status}`,
          message:
            typeof error?.message === "string"
              ? error.message
              : `上传失败（HTTP ${xhr.status}）`,
          status: xhr.status,
          details:
            error?.details && typeof error.details === "object"
              ? (error.details as Record<string, unknown>)
              : undefined,
          traceId: typeof error?.trace_id === "string" ? error.trace_id : undefined,
          retryable:
            typeof error?.retryable === "boolean" ? error.retryable : xhr.status >= 500,
        }),
      );
    };

    xhr.onerror = () => {
      reject(
        new ApiError({
          code: "NETWORK",
          message: "上传网络中断，请重试",
          status: 0,
          retryable: true,
        }),
      );
    };

    xhr.ontimeout = () => {
      reject(
        new ApiError({
          code: "UPLOAD_TIMEOUT",
          message: "上传超时，请检查网络或减小文件",
          status: 0,
          retryable: true,
        }),
      );
    };

    xhr.onabort = () => reject(new DOMException("上传已取消", "AbortError"));

    options.signal?.addEventListener("abort", () => xhr.abort(), { once: true });

    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
}

export async function uploadDocument(
  kbId: string,
  file: File,
  options: UploadOptions = {},
): Promise<UploadAccepted> {
  try {
    return await sendUpload(kbId, file, options);
  } catch (error) {
    // 上传是用户动作，只对过期 token 重放一次；文件对象可复用。
    if (isApiError(error) && error.code === "TOKEN_EXPIRED" && (await refreshOnce())) {
      return sendUpload(kbId, file, options);
    }
    throw error;
  }
}
