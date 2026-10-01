from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AiError(_message.Message):
    __slots__ = ("code", "message", "http_status", "retryable", "trace_id", "details_json")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    HTTP_STATUS_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    TRACE_ID_FIELD_NUMBER: _ClassVar[int]
    DETAILS_JSON_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    http_status: int
    retryable: bool
    trace_id: str
    details_json: str
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., http_status: _Optional[int] = ..., retryable: _Optional[bool] = ..., trace_id: _Optional[str] = ..., details_json: _Optional[str] = ...) -> None: ...

class ChatMessage(_message.Message):
    __slots__ = ("role", "content")
    ROLE_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    role: str
    content: str
    def __init__(self, role: _Optional[str] = ..., content: _Optional[str] = ...) -> None: ...

class ChatRequest(_message.Message):
    __slots__ = ("query", "conversation_id", "history", "use_rag", "kb_ids", "use_memory", "use_tools", "model", "temperature", "top_k", "rerank_top_n", "score_threshold", "metadata")
    class MetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    HISTORY_FIELD_NUMBER: _ClassVar[int]
    USE_RAG_FIELD_NUMBER: _ClassVar[int]
    KB_IDS_FIELD_NUMBER: _ClassVar[int]
    USE_MEMORY_FIELD_NUMBER: _ClassVar[int]
    USE_TOOLS_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    TEMPERATURE_FIELD_NUMBER: _ClassVar[int]
    TOP_K_FIELD_NUMBER: _ClassVar[int]
    RERANK_TOP_N_FIELD_NUMBER: _ClassVar[int]
    SCORE_THRESHOLD_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    query: str
    conversation_id: str
    history: _containers.RepeatedCompositeFieldContainer[ChatMessage]
    use_rag: bool
    kb_ids: _containers.RepeatedScalarFieldContainer[str]
    use_memory: bool
    use_tools: bool
    model: str
    temperature: float
    top_k: int
    rerank_top_n: int
    score_threshold: float
    metadata: _containers.ScalarMap[str, str]
    def __init__(self, query: _Optional[str] = ..., conversation_id: _Optional[str] = ..., history: _Optional[_Iterable[_Union[ChatMessage, _Mapping]]] = ..., use_rag: _Optional[bool] = ..., kb_ids: _Optional[_Iterable[str]] = ..., use_memory: _Optional[bool] = ..., use_tools: _Optional[bool] = ..., model: _Optional[str] = ..., temperature: _Optional[float] = ..., top_k: _Optional[int] = ..., rerank_top_n: _Optional[int] = ..., score_threshold: _Optional[float] = ..., metadata: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ChatResponse(_message.Message):
    __slots__ = ("answer", "conversation_id", "message_id", "references", "tool_calls", "usage", "finish_reason", "model", "degraded", "degraded_reasons", "elapsed_ms")
    ANSWER_FIELD_NUMBER: _ClassVar[int]
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    REFERENCES_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALLS_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_REASONS_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    answer: str
    conversation_id: str
    message_id: str
    references: _containers.RepeatedCompositeFieldContainer[Reference]
    tool_calls: _containers.RepeatedCompositeFieldContainer[ToolCallTrace]
    usage: Usage
    finish_reason: str
    model: str
    degraded: bool
    degraded_reasons: _containers.RepeatedScalarFieldContainer[str]
    elapsed_ms: int
    def __init__(self, answer: _Optional[str] = ..., conversation_id: _Optional[str] = ..., message_id: _Optional[str] = ..., references: _Optional[_Iterable[_Union[Reference, _Mapping]]] = ..., tool_calls: _Optional[_Iterable[_Union[ToolCallTrace, _Mapping]]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., finish_reason: _Optional[str] = ..., model: _Optional[str] = ..., degraded: _Optional[bool] = ..., degraded_reasons: _Optional[_Iterable[str]] = ..., elapsed_ms: _Optional[int] = ...) -> None: ...

class Reference(_message.Message):
    __slots__ = ("index", "chunk_id", "doc_id", "kb_id", "doc_name", "page", "heading_path", "score", "snippet", "content_sha256")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    CHUNK_ID_FIELD_NUMBER: _ClassVar[int]
    DOC_ID_FIELD_NUMBER: _ClassVar[int]
    KB_ID_FIELD_NUMBER: _ClassVar[int]
    DOC_NAME_FIELD_NUMBER: _ClassVar[int]
    PAGE_FIELD_NUMBER: _ClassVar[int]
    HEADING_PATH_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    SNIPPET_FIELD_NUMBER: _ClassVar[int]
    CONTENT_SHA256_FIELD_NUMBER: _ClassVar[int]
    index: int
    chunk_id: str
    doc_id: str
    kb_id: str
    doc_name: str
    page: int
    heading_path: str
    score: float
    snippet: str
    content_sha256: str
    def __init__(self, index: _Optional[int] = ..., chunk_id: _Optional[str] = ..., doc_id: _Optional[str] = ..., kb_id: _Optional[str] = ..., doc_name: _Optional[str] = ..., page: _Optional[int] = ..., heading_path: _Optional[str] = ..., score: _Optional[float] = ..., snippet: _Optional[str] = ..., content_sha256: _Optional[str] = ...) -> None: ...

class ToolCallTrace(_message.Message):
    __slots__ = ("call_id", "name", "arguments_json", "status", "summary", "elapsed_ms")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_JSON_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    name: str
    arguments_json: str
    status: str
    summary: str
    elapsed_ms: int
    def __init__(self, call_id: _Optional[str] = ..., name: _Optional[str] = ..., arguments_json: _Optional[str] = ..., status: _Optional[str] = ..., summary: _Optional[str] = ..., elapsed_ms: _Optional[int] = ...) -> None: ...

class Usage(_message.Message):
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")
    PROMPT_TOKENS_FIELD_NUMBER: _ClassVar[int]
    COMPLETION_TOKENS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_TOKENS_FIELD_NUMBER: _ClassVar[int]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    def __init__(self, prompt_tokens: _Optional[int] = ..., completion_tokens: _Optional[int] = ..., total_tokens: _Optional[int] = ...) -> None: ...

class ChatEvent(_message.Message):
    __slots__ = ("meta", "reference", "token", "tool_call", "tool_result", "usage", "error", "done", "unknown")
    META_FIELD_NUMBER: _ClassVar[int]
    REFERENCE_FIELD_NUMBER: _ClassVar[int]
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOOL_CALL_FIELD_NUMBER: _ClassVar[int]
    TOOL_RESULT_FIELD_NUMBER: _ClassVar[int]
    USAGE_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    DONE_FIELD_NUMBER: _ClassVar[int]
    UNKNOWN_FIELD_NUMBER: _ClassVar[int]
    meta: StreamMeta
    reference: StreamReferences
    token: StreamToken
    tool_call: StreamToolCall
    tool_result: StreamToolResult
    usage: Usage
    error: StreamError
    done: StreamDone
    unknown: StreamUnknown
    def __init__(self, meta: _Optional[_Union[StreamMeta, _Mapping]] = ..., reference: _Optional[_Union[StreamReferences, _Mapping]] = ..., token: _Optional[_Union[StreamToken, _Mapping]] = ..., tool_call: _Optional[_Union[StreamToolCall, _Mapping]] = ..., tool_result: _Optional[_Union[StreamToolResult, _Mapping]] = ..., usage: _Optional[_Union[Usage, _Mapping]] = ..., error: _Optional[_Union[StreamError, _Mapping]] = ..., done: _Optional[_Union[StreamDone, _Mapping]] = ..., unknown: _Optional[_Union[StreamUnknown, _Mapping]] = ...) -> None: ...

class StreamMeta(_message.Message):
    __slots__ = ("conversation_id", "message_id", "model", "created_at", "degraded", "degraded_reasons")
    CONVERSATION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    MODEL_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_REASONS_FIELD_NUMBER: _ClassVar[int]
    conversation_id: str
    message_id: str
    model: str
    created_at: str
    degraded: bool
    degraded_reasons: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, conversation_id: _Optional[str] = ..., message_id: _Optional[str] = ..., model: _Optional[str] = ..., created_at: _Optional[str] = ..., degraded: _Optional[bool] = ..., degraded_reasons: _Optional[_Iterable[str]] = ...) -> None: ...

class StreamReferences(_message.Message):
    __slots__ = ("references",)
    REFERENCES_FIELD_NUMBER: _ClassVar[int]
    references: _containers.RepeatedCompositeFieldContainer[Reference]
    def __init__(self, references: _Optional[_Iterable[_Union[Reference, _Mapping]]] = ...) -> None: ...

class StreamToken(_message.Message):
    __slots__ = ("delta",)
    DELTA_FIELD_NUMBER: _ClassVar[int]
    delta: str
    def __init__(self, delta: _Optional[str] = ...) -> None: ...

class StreamToolCall(_message.Message):
    __slots__ = ("call_id", "name", "arguments_json")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ARGUMENTS_JSON_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    name: str
    arguments_json: str
    def __init__(self, call_id: _Optional[str] = ..., name: _Optional[str] = ..., arguments_json: _Optional[str] = ...) -> None: ...

class StreamToolResult(_message.Message):
    __slots__ = ("call_id", "name", "status", "summary", "elapsed_ms")
    CALL_ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    SUMMARY_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    call_id: str
    name: str
    status: str
    summary: str
    elapsed_ms: int
    def __init__(self, call_id: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., summary: _Optional[str] = ..., elapsed_ms: _Optional[int] = ...) -> None: ...

class StreamError(_message.Message):
    __slots__ = ("code", "message", "retryable")
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    RETRYABLE_FIELD_NUMBER: _ClassVar[int]
    code: str
    message: str
    retryable: bool
    def __init__(self, code: _Optional[str] = ..., message: _Optional[str] = ..., retryable: _Optional[bool] = ...) -> None: ...

class StreamDone(_message.Message):
    __slots__ = ("finish_reason", "elapsed_ms", "partial")
    FINISH_REASON_FIELD_NUMBER: _ClassVar[int]
    ELAPSED_MS_FIELD_NUMBER: _ClassVar[int]
    PARTIAL_FIELD_NUMBER: _ClassVar[int]
    finish_reason: str
    elapsed_ms: int
    partial: bool
    def __init__(self, finish_reason: _Optional[str] = ..., elapsed_ms: _Optional[int] = ..., partial: _Optional[bool] = ...) -> None: ...

class StreamUnknown(_message.Message):
    __slots__ = ("event", "data_json")
    EVENT_FIELD_NUMBER: _ClassVar[int]
    DATA_JSON_FIELD_NUMBER: _ClassVar[int]
    event: str
    data_json: bytes
    def __init__(self, event: _Optional[str] = ..., data_json: _Optional[bytes] = ...) -> None: ...
