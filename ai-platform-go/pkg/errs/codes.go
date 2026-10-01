// Package errs 定义网关的错误码、统一错误信封与 HTTP 映射（权威定义：docs/02 §4）。
//
// 码分两区（docs/02 §4.1/§4.2）：网关自有码的状态码固定；复用 ai-platform 的码
// 网关 MUST 原样透传（接缝 J2），状态码从上游响应取，不得改写成网关自己的码。
package errs

// Code 是机器可读的错误码（写进 error.code）。
type Code string

// 网关自有错误码（docs/02-§4.1）。
const (
	// CodeInvalidArgument 表示请求参数不合法；响应 MUST 带字段级 details。
	CodeInvalidArgument Code = "INVALID_ARGUMENT"
	// CodeUnauthenticated 表示缺少或无法识别的身份凭证（不含「已过期」）。
	CodeUnauthenticated Code = "UNAUTHENTICATED"
	// CodeTokenExpired 单独成码，让客户端能据此触发一次静默刷新。
	CodeTokenExpired Code = "TOKEN_EXPIRED"
	// CodeInvalidCredentials 覆盖「账号不存在」与「密码错」两情形，防用户枚举。
	CodeInvalidCredentials Code = "INVALID_CREDENTIALS"
	// CodeInvalidRefreshToken 表示刷新令牌不存在、已作废、已被用过或已过期。
	CodeInvalidRefreshToken Code = "INVALID_REFRESH_TOKEN"
	// CodePermissionDenied 表示身份合法但无权访问该资源。
	CodePermissionDenied Code = "PERMISSION_DENIED"
	// CodeUserDisabled 表示账号被禁用；客户端 MUST 清本地登录态、不得重试。
	CodeUserDisabled Code = "USER_DISABLED"
	// CodeResourceNotFound 是「资源不存在」的兜底码。
	CodeResourceNotFound Code = "RESOURCE_NOT_FOUND"
	// CodeConversationNotFound 是会话维度的 404，便于前端做定向提示。
	CodeConversationNotFound Code = "CONVERSATION_NOT_FOUND"
	// CodeMessageNotFound 是消息维度的 404。
	CodeMessageNotFound Code = "MESSAGE_NOT_FOUND"
	// CodeConflict 是状态冲突的兜底码（正常路径应使用更具体的码）。
	CodeConflict Code = "CONFLICT"
	// CodeEmailAlreadyExists 表示邮箱被占用；查重含已软删行，邮箱永久占用。
	CodeEmailAlreadyExists Code = "EMAIL_ALREADY_EXISTS"
	// CodeConversationArchived 表示往已归档会话发消息被拒。
	CodeConversationArchived Code = "CONVERSATION_ARCHIVED"
	// CodePayloadTooLarge 表示请求体超过大小上限。
	CodePayloadTooLarge Code = "PAYLOAD_TOO_LARGE"
	// CodeUnsupportedMediaType 表示 Content-Type 不在白名单内。
	CodeUnsupportedMediaType Code = "UNSUPPORTED_MEDIA_TYPE"
	// CodeQuotaExceeded 表示配额耗尽；不可重试，需先升级或等到下个周期。
	CodeQuotaExceeded Code = "QUOTA_EXCEEDED"
	// CodeRateLimited 是限流码：唯一「等一会儿重试就有意义」的 429。
	CodeRateLimited Code = "RATE_LIMITED"
	// CodeInternalError 是本服务自身缺陷导致的 500（依赖故障应归 503 类码）。
	CodeInternalError Code = "INTERNAL_ERROR"
	// CodeAIUnavailable 表示 AI 服务不可达；可重试。
	CodeAIUnavailable Code = "AI_UNAVAILABLE"
	// CodeAIOverloaded 表示 AI 服务过载，与不可达区分以便分级告警。
	CodeAIOverloaded Code = "AI_OVERLOADED"
	// CodeDependencyUnavailable 是 MySQL/Redis 等基础设施不可用的通用码。
	CodeDependencyUnavailable Code = "DEPENDENCY_UNAVAILABLE"
	// CodeAITimeout 表示 AI 侧响应超时；与 502 区分以便客户端选择更长超时重试。
	CodeAITimeout Code = "AI_TIMEOUT"
	// CodeServiceShuttingDown 表示优雅停机中；客户端应换实例重试。
	CodeServiceShuttingDown Code = "SERVICE_SHUTTING_DOWN"
)

// CodeInvalidToken 是「令牌无效」的兜底码：契约只区分 TOKEN_EXPIRED 与
// UNAUTHENTICATED，签名错 / ver 不匹配 / token_type 错都归后者，
// 过期单独用 TOKEN_EXPIRED 以便客户端触发静默刷新。
const CodeInvalidToken = CodeUnauthenticated

// spec 描述一个错误码的固定元信息。
type spec struct {
	status    int    // HTTP 状态码
	retryable bool   // 客户端可否安全重试
	message   string // 默认 zh-CN 文案（可被具体错误覆盖）
}

// specs 是 §4.1 的完整映射表，MUST 与 docs/02-§4.1 逐行一致（测试 TestSpecsMatchContract 守着它）。
var specs = map[Code]spec{
	CodeInvalidArgument:       {400, false, "请求参数不合法"},
	CodeUnauthenticated:       {401, false, "身份未认证"},
	CodeTokenExpired:          {401, false, "登录已过期"},
	CodeInvalidCredentials:    {401, false, "账号或密码错误"},
	CodeInvalidRefreshToken:   {401, false, "刷新令牌无效"},
	CodePermissionDenied:      {403, false, "无权访问该资源"},
	CodeUserDisabled:          {403, false, "账号已被禁用"},
	CodeResourceNotFound:      {404, false, "资源不存在"},
	CodeConversationNotFound:  {404, false, "会话不存在"},
	CodeMessageNotFound:       {404, false, "消息不存在"},
	CodeConflict:              {409, false, "资源冲突"},
	CodeEmailAlreadyExists:    {409, false, "该邮箱已被注册"},
	CodeConversationArchived:  {409, false, "会话已归档，不可发送消息"},
	CodePayloadTooLarge:       {413, false, "请求体过大"},
	CodeUnsupportedMediaType:  {415, false, "不支持的内容类型"},
	CodeQuotaExceeded:         {429, false, "配额已用完"},
	CodeRateLimited:           {429, true, "请求过于频繁"},
	CodeInternalError:         {500, true, "服务内部错误"},
	CodeAIUnavailable:         {503, true, "AI 服务暂时不可用，请稍后重试"},
	CodeAIOverloaded:          {503, true, "AI 服务繁忙"},
	CodeDependencyUnavailable: {503, true, "依赖服务不可用"},
	CodeAITimeout:             {504, true, "AI 服务响应超时"},
	CodeServiceShuttingDown:   {503, true, "服务正在关闭，请稍后重试"},
}

// aiErrorStatus 是「复用 ai-platform 的错误码」的兜底状态码（docs/02-§4.2）。
// 正常路径下透传用的是上游真实状态码；这张表只在需要按码复原状态码时使用
// （例如从持久化的幂等响应快照还原）。值必须与 §4.2 表一致。
var aiErrorStatus = map[Code]int{
	"UPSTREAM_LLM_ERROR":        502,
	"UPSTREAM_LLM_AUTH_ERROR":   502,
	"UPSTREAM_TIMEOUT":          504,
	"CONTEXT_TOO_LONG":          400,
	"QUERY_EMPTY":               400,
	"CONTENT_FILTERED":          400,
	"KB_NOT_FOUND":              404,
	"DOCUMENT_NOT_FOUND":        404,
	"TASK_NOT_FOUND":            404,
	"MEMORY_NOT_FOUND":          404,
	"KB_NAME_CONFLICT":          409,
	"DOCUMENT_DUPLICATE":        409,
	"KB_NOT_EMPTY":              409,
	"TASK_NOT_CANCELABLE":       409,
	"TASK_NOT_RETRYABLE":        409,
	"FILE_TOO_LARGE":            413,
	"UNSUPPORTED_FILE_TYPE":     415,
	"UNPROCESSABLE_DOCUMENT":    422,
	"TOOL_NOT_FOUND":            404,
	"TOOL_FORBIDDEN":            403,
	"TOOL_EXECUTION_FAILED":     502,
	"TOOL_TIMEOUT":              504,
	"MCP_SERVER_NOT_FOUND":      404,
	"MCP_SERVER_UNAVAILABLE":    503,
	"UPSTREAM_MCP_ERROR":        502,
	"RETRIEVAL_FAILED":          503,
	"OVERLOADED":                503,
	"SUMMARY_UNAVAILABLE":       404,
	"SUMMARY_GENERATION_FAILED": 502,
}

// LookupStatus 返回某错误码的 HTTP 状态码。
// 第二个返回值表示该码是否为已知的网关自有码。未知码（含 AI 侧透传的码）返回 500，
// 调用方应优先使用上游真实状态码。
func LookupStatus(code Code) (int, bool) {
	if s, ok := specs[code]; ok {
		return s.status, true
	}
	if st, ok := aiErrorStatus[code]; ok {
		return st, false
	}
	return 500, false
}

// KnownCode 报告 code 是否在 §4.1 或 §4.2 的表里。
func KnownCode(code Code) bool {
	if _, ok := specs[code]; ok {
		return true
	}
	_, ok := aiErrorStatus[code]
	return ok
}

// Codes 返回全部网关自有错误码（供契约测试遍历）。
func Codes() []Code {
	out := make([]Code, 0, len(specs))
	for c := range specs {
		out = append(out, c)
	}
	return out
}

// AIErrorCodes 返回全部复用 ai-platform 的错误码（供契约测试遍历）。
func AIErrorCodes() []Code {
	out := make([]Code, 0, len(aiErrorStatus))
	for c := range aiErrorStatus {
		out = append(out, c)
	}
	return out
}
