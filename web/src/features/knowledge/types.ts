export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  chunk_size: number;
  chunk_overlap: number;
  embedding_model: string | null;
  embedding_dim: number;
  retrieval_top_k: number;
  rerank_top_n: number;
  score_threshold: number;
  status: string;
  document_count: number;
  chunk_count: number;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeBaseInput {
  name: string;
  description?: string;
  chunk_size?: number;
  chunk_overlap?: number;
  retrieval_top_k?: number;
  rerank_top_n?: number;
  score_threshold?: number;
}

export type DocumentStatus =
  | "PENDING"
  | "PARSING"
  | "CHUNKING"
  | "EMBEDDING"
  | "INDEXED"
  | "FAILED"
  | "DELETING"
  | "DELETED"
  | string;

export interface DocumentError {
  code: string;
  message: string;
}

export interface KnowledgeDocument {
  id: string;
  kb_id: string;
  doc_name: string;
  file_ext: string;
  mime_type: string;
  size_bytes: number;
  status: DocumentStatus;
  page_count: number | null;
  chunk_count: number;
  char_count: number;
  chunks_total: number | null;
  truncated: boolean;
  chunk_size: number;
  chunk_overlap: number;
  content_sha256: string;
  task_id: string | null;
  error: DocumentError | null;
  metadata: Record<string, unknown>;
  indexed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface DocumentChunk {
  chunk_id: string;
  doc_id: string;
  kb_id: string;
  chunk_index: number;
  content: string;
  content_sha256: string;
  char_start: number;
  char_end: number;
  token_count: number;
  page: number | null;
  heading_path: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface UploadAccepted {
  doc_id: string;
  task_id: string | null;
  status: string;
  doc_name: string;
  content_sha256: string;
  duplicated: boolean;
  created_at: string;
}

export interface SearchRequest {
  query: string;
  top_k?: number;
  rerank_top_n?: number;
  score_threshold?: number;
  with_rerank?: boolean;
}

export interface SearchItem {
  index: number;
  chunk_id: string;
  doc_id: string;
  doc_name: string;
  page: number | null;
  heading_path: string | null;
  vector_score: number;
  rerank_score: number | null;
  score: number;
  merged: boolean;
  content: string;
}

export interface SearchResponse {
  query: string;
  recalled: number;
  returned: number;
  rerank_used: boolean;
  elapsed_ms: number;
  items: SearchItem[];
}

export interface DocumentFilters {
  status?: string;
  limit?: number;
  cursor?: string | null;
}

export interface KnowledgeBaseFilters {
  limit?: number;
  cursor?: string | null;
}
