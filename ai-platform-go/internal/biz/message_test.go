package biz

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// newMsgFixture 组装一个「已存在且 active」的会话 + 假仓储 + 假编排器。
func newMsgFixture(t *testing.T) (*MessageService, *fakeConvRepo, *fakeMsgRepo, *fakeOrchestrator, *Conversation) {
	t.Helper()

	conv := &Conversation{
		ID:          "cv_01J8ZQ3K7N9P2V6R4T8W1Y5B3C",
		UserID:      "u_1",
		Status:      ConversationStatusActive,
		TitleSource: TitleSourceAuto,
		CreatedAt:   testNow(),
	}
	convs := newFakeConvRepo(conv)
	msgs := newFakeMsgRepo()
	orch := &fakeOrchestrator{result: &ChatResult{
		Content:      "这是回答",
		FinishReason: FinishReasonStop,
		Model:        "deepseek-flash",
		Usage:        &MessageUsage{PromptTokens: 10, CompletionTokens: 20, TotalTokens: 30},
		ElapsedMS:    1234,
	}}

	svc := NewMessageService(MessageDeps{
		Conversations:     convs,
		Messages:          msgs,
		Orchestrator:      orch,
		Clock:             testNow,
		AutoTitleMaxChars: AutoTitleMaxCharsDefault,
	})
	return svc, convs, msgs, orch, conv
}

func sendInput(content string) SendMessageInput { return SendMessageInput{Content: content} }

// ---- 正常路径（docs/03-§5 的落库顺序）----

func TestSendPersistsUserThenAssistant(t *testing.T) {
	svc, convs, msgs, orch, conv := newMsgFixture(t)

	got, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("  你好\n\n世界  "), RequestMeta{TraceID: "trace-1"})
	if err != nil {
		t.Fatalf("Send 失败: %v", err)
	}

	if got.User == nil || got.Assistant == nil {
		t.Fatal("非流式路径必须同时返回 user 与 assistant 两条消息")
	}
	if got.User.Seq != 1 || got.Assistant.Seq != 2 {
		t.Fatalf("seq 应为 1、2，实际 %d、%d", got.User.Seq, got.Assistant.Seq)
	}
	if got.User.Role != MessageRoleUser || got.Assistant.Role != MessageRoleAssistant {
		t.Fatalf("角色错误: %q / %q", got.User.Role, got.Assistant.Role)
	}
	if got.User.Content != "你好\n\n世界" || got.Assistant.Content != "这是回答" {
		t.Fatalf("正文错误: 用户 %q，助手 %q", got.User.Content, got.Assistant.Content)
	}
	if got.User.TraceID == nil || *got.User.TraceID != "trace-1" {
		t.Error("user 消息应记录 trace_id")
	}
	if got.Assistant.Usage == nil || got.Assistant.Usage.TotalTokens != 30 {
		t.Errorf("assistant 消息应带 token 用量（配额要靠它累加），实际 %#v", got.Assistant.Usage)
	}
	if got.Assistant.ElapsedMS == nil || *got.Assistant.ElapsedMS != 1234 {
		t.Errorf("elapsed_ms 应透传，实际 %#v", got.Assistant.ElapsedMS)
	}

	// 落库顺序不可调换：先 user 后 assistant。
	if len(msgs.appended) != 2 || msgs.appended[0].ID != got.User.ID || msgs.appended[1].ID != got.Assistant.ID {
		t.Fatalf("落库顺序应为 user → assistant，实际 %#v", msgs.appended)
	}
	// 自动标题：首条消息落库后写入。
	if convs.autoTitleCalls != 1 {
		t.Errorf("首条消息应触发一次自动标题，实际 %d 次", convs.autoTitleCalls)
	}
	if conv.Title != "你好 世界" {
		t.Errorf("会话标题应为自动生成的 %q，实际 %q", "你好 世界", conv.Title)
	}
	if orch.calls != 1 {
		t.Errorf("编排器应被调用 1 次，实际 %d 次", orch.calls)
	}
}

// ---- 归档 / 不存在（REQ-CONV-003 / AC-CONV-02）----

func TestSendArchivedConversationReturns409(t *testing.T) {
	svc, _, msgs, orch, conv := newMsgFixture(t)
	conv.Status = ConversationStatusArchived

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	// 409 而不是 404：会话对用户可见，说「不存在」会让他以为被删了。
	assertAppError(t, err, errs.CodeConversationArchived, 409)
	if len(msgs.appended) != 0 {
		t.Fatal("归档会话必须可读不可写，不得落库任何消息")
	}
	if orch.calls != 0 {
		t.Fatal("归档会话不得调用 AI（会白烧配额）")
	}
}

func TestSendMissingConversationReturns404(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	_, err := svc.Send(context.Background(), "u_other", conv.ID, sendInput("你好"), RequestMeta{})
	assertAppError(t, err, errs.CodeConversationNotFound, 404)
	if len(msgs.appended) != 0 {
		t.Fatal("越权写入必须为 0 行")
	}
}

// TestSendArchiveRaceMapsTo409 覆盖「读到 active 后、分配 seq 前被归档」的竞态：
// 分配 seq 的 SQL 带 status='active' 条件，0 行时返回 ErrConversationArchived。
func TestSendArchiveRaceMapsTo409(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)
	msgs.appendErr = ErrConversationArchived

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	assertAppError(t, err, errs.CodeConversationArchived, 409)
}

// ---- 编排未接线（M2 的有意状态）----

func TestSendWithoutOrchestratorPersistsUserThenFails(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)
	svc.d.Orchestrator = nil

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	assertAppError(t, err, errs.CodeAIUnavailable, 503)

	appErr, _ := errs.As(err)
	if appErr.Details()["reason"] != "orchestrator_not_configured" {
		t.Fatalf("details.reason 应为 orchestrator_not_configured，实际 %#v", appErr.Details())
	}

	// 关键：user 消息必须已经落库 —— 否则「AI 挂了」会把用户的提问一起弄丢。
	if len(msgs.appended) != 1 || msgs.appended[0].Role != MessageRoleUser {
		t.Fatalf("编排未接线时 user 消息仍应落库，实际 %#v", msgs.appended)
	}
}

// TestSendAssistantPersistFailureIsReported 非流式路径不得「回答落库失败也返回成功」：
// 响应里的消息必须是能从 GET /messages/{id} 取到的那一条。
func TestSendAssistantPersistFailureIsReported(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)
	msgs.failAppendAfter = 2 // 第 1 次（user）成功，第 2 次（assistant）失败

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	if err == nil {
		t.Fatal("assistant 落库失败必须报错，否则客户端刷新后会发现回答不见了")
	}
	if len(msgs.appended) != 1 {
		t.Fatalf("只有 user 消息应该落库成功，实际 %d 条", len(msgs.appended))
	}
}

func TestSendOrchestratorErrorIsReturnedAsIs(t *testing.T) {
	svc, _, _, orch, conv := newMsgFixture(t)
	// UPSTREAM_LLM_ERROR 是「原样透传上游」的码，只以字符串形式存在
	// （网关不认识它的语义，只负责转发，见 pkg/errs/codes.go 的状态码表）。
	upstream := errs.New(errs.Code("UPSTREAM_LLM_ERROR")).
		WithDetail("gateway", map[string]any{"upstream": "ai-platform"})
	orch.err = upstream

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	if err != upstream {
		t.Fatalf("上游错误应原样透传（不重新包装），实际 %v", err)
	}
}

func TestSendEmptyOrchestratorResultIsUpstreamFailure(t *testing.T) {
	svc, _, _, orch, conv := newMsgFixture(t)
	orch.result = nil

	_, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	assertAppError(t, err, errs.CodeAIUnavailable, 503)
	appErr, _ := errs.As(err)
	if appErr.Details()["reason"] != "empty_result" {
		t.Fatalf("details.reason 应为 empty_result，实际 %#v", appErr.Details())
	}
}

// ---- 自动标题的触发条件（AC-CONV-04）----

func TestSendSkipsAutoTitleWhenTitleAlreadySet(t *testing.T) {
	svc, convs, _, _, conv := newMsgFixture(t)
	conv.Title = "已有标题"
	conv.TitleSource = TitleSourceAuto

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("新提问"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if convs.autoTitleCalls != 0 {
		t.Fatalf("标题非空时不该再生成，实际调用 %d 次", convs.autoTitleCalls)
	}
}

func TestSendSkipsAutoTitleWhenManual(t *testing.T) {
	svc, convs, _, _, conv := newMsgFixture(t)
	conv.TitleSource = TitleSourceManual

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("新提问"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if convs.autoTitleCalls != 0 {
		t.Fatalf("手工标题之后 MUST NOT 再被自动覆盖（AC-CONV-04），实际调用 %d 次", convs.autoTitleCalls)
	}
}

// TestSendAutoTitleFailureDoesNotFailSend 标题只是展示信息，失败不该让提问失败。
func TestSendAutoTitleFailureDoesNotFailSend(t *testing.T) {
	svc, convs, _, _, conv := newMsgFixture(t)
	convs.autoTitleErr = errInvalidServer

	got, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{})
	if err != nil {
		t.Fatalf("标题生成失败不该让 Send 失败，实际 %v", err)
	}
	if got.Assistant == nil {
		t.Fatal("回答仍应正常写入")
	}
}

// TestSendAutoTitleNotAppliedKeepsTitle 覆盖「SQL 条件不成立（并发下用户刚改过标题）」：
// applied=false 时服务层不得回填标题。
func TestSendAutoTitleNotAppliedKeepsTitle(t *testing.T) {
	svc, convs, _, _, conv := newMsgFixture(t)
	convs.autoTitleDone = false

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if conv.Title != "" {
		t.Fatalf("applied=false 时不该回填标题，实际 %q", conv.Title)
	}
}

// ---- 入参校验 ----

func TestSendContentValidation(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	tooLong := strings.Repeat("测", MessageContentMaxRunes+1)
	for name, in := range map[string]SendMessageInput{
		"空串":  sendInput(""),
		"纯空白": sendInput("   \n\t "),
		"超长":  sendInput(tooLong),
	} {
		t.Run(name, func(t *testing.T) {
			_, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{})
			assertFieldError(t, err, "content")
		})
	}
	if len(msgs.appended) != 0 {
		t.Fatalf("校验失败时不该落库，实际 %d 条", len(msgs.appended))
	}
}

func TestSendNumericRangeValidation(t *testing.T) {
	svc, _, _, _, conv := newMsgFixture(t)

	temp := 2.5
	threshold := 1.5
	topK := 0
	rerank := -1
	cases := map[string]SendMessageInput{
		"temperature":     {Content: "你好", Temperature: &temp},
		"score_threshold": {Content: "你好", ScoreThreshold: &threshold},
		"top_k":           {Content: "你好", TopK: &topK},
		"rerank_top_n":    {Content: "你好", RerankTopN: &rerank},
	}
	for field, in := range cases {
		t.Run(field, func(t *testing.T) {
			_, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{})
			assertFieldError(t, err, field)
		})
	}
}

// TestSendAttachmentsRejectedAttachments 附件在 M2 未支持时必须**报错**而不是静默忽略：
// 忽略会让用户以为模型看过那个文件。
func TestSendAttachmentsRejected(t *testing.T) {
	svc, _, _, _, conv := newMsgFixture(t)

	in := sendInput("你好")
	in.Attachments = []json.RawMessage{json.RawMessage(`{"doc_id":"doc_1"}`)}

	_, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{})
	appErr, ok := errs.As(err)
	if !ok || appErr.Code() != errs.CodeInvalidArgument {
		t.Fatalf("期望 400 INVALID_ARGUMENT，实际 %v", err)
	}
	fields, _ := appErr.Details()["fields"].([]map[string]any)
	if len(fields) == 0 || fields[0]["reason"] != "not_supported" {
		t.Fatalf("reason 应为 not_supported，实际 %#v", appErr.Details()["fields"])
	}
}

// ---- 会话默认值与本轮覆盖的合并（docs/03-§4.3）----

func TestSendMergesConversationDefaultsWithOverrides(t *testing.T) {
	svc, _, _, orch, conv := newMsgFixture(t)

	convModel := "deepseek-v4-pro"
	conv.Model = &convModel
	conv.KBIDs = []string{"kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C"}

	override := "deepseek-flash"
	rag := false
	in := SendMessageInput{
		Content: "你好",
		Model:   &override,
		UseRAG:  &rag,
	}
	if _, err := svc.Send(context.Background(), "u_1", conv.ID, in, RequestMeta{TraceID: "trace-9"}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}

	if orch.got.Model != "deepseek-flash" {
		t.Errorf("请求体 model 应覆盖会话默认值，实际 %q", orch.got.Model)
	}
	if orch.got.UseRAG {
		t.Error("显式 use_rag=false 不得被默认值 true 覆盖")
	}
	if !orch.got.UseMemory {
		t.Error("use_memory 缺省应为 true")
	}
	if orch.got.UseTools {
		t.Error("use_tools 缺省应为 false")
	}
	if len(orch.got.KBIDs) != 1 || orch.got.KBIDs[0] != conv.KBIDs[0] {
		t.Errorf("kb_ids 应回落到会话级设置，实际 %#v", orch.got.KBIDs)
	}
	if orch.got.TraceID != "trace-9" {
		t.Errorf("trace_id 应透传给 AI，实际 %q", orch.got.TraceID)
	}
}

func TestSendFallsBackToConversationModel(t *testing.T) {
	svc, _, _, orch, conv := newMsgFixture(t)
	convModel := "deepseek-v4-pro"
	conv.Model = &convModel

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	if orch.got.Model != "deepseek-v4-pro" {
		t.Errorf("未传 model 时应回落到会话级模型，实际 %q", orch.got.Model)
	}
}

// ---- 列表 / 详情 ----

// TestListChecksConversationFirst 是「软删会话的消息也必须 404」的落点：
// 只靠 JOIN 过滤的话，「会话不存在」与「会话没有消息」都会返回空列表。
func TestListChecksConversationFirst(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	_, err := svc.List(context.Background(), "u_1", conv.ID, ListMessagesInput{
		PaginationInput: PaginationInput{Limit: 20},
		Order:           OrderDesc,
	})
	if err != nil {
		t.Fatalf("List 失败: %v", err)
	}
	if msgs.listCalls != 1 {
		t.Fatalf("正常路径应查询消息，实际 %d 次", msgs.listCalls)
	}

	_, err = svc.List(context.Background(), "u_other", conv.ID, ListMessagesInput{
		PaginationInput: PaginationInput{Limit: 20},
		Order:           OrderDesc,
	})
	assertAppError(t, err, errs.CodeConversationNotFound, 404)
	if msgs.listCalls != 1 {
		t.Fatal("会话不存在时应直接 404，不该去查消息列表")
	}
}

func TestListMessagesValidation(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	_, err := svc.List(context.Background(), "u_1", conv.ID, ListMessagesInput{
		PaginationInput: PaginationInput{Limit: 0},
		Order:           OrderDesc,
	})
	assertFieldError(t, err, "limit")

	_, err = svc.List(context.Background(), "u_1", conv.ID, ListMessagesInput{
		PaginationInput: PaginationInput{Limit: 20},
		Order:           "newest",
	})
	assertFieldError(t, err, "order")

	if msgs.listCalls != 0 {
		t.Fatalf("校验失败时不该查询消息，实际 %d 次", msgs.listCalls)
	}
}

func TestGetMessageCrossUserReturns404(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	if _, err := svc.Send(context.Background(), "u_1", conv.ID, sendInput("你好"), RequestMeta{}); err != nil {
		t.Fatalf("Send 失败: %v", err)
	}
	id := msgs.appended[0].ID

	_, err := svc.Get(context.Background(), "u_other", id)
	assertAppError(t, err, errs.CodeMessageNotFound, 404)

	got, err := svc.Get(context.Background(), "u_1", id)
	if err != nil {
		t.Fatalf("本人读取应成功，实际 %v", err)
	}
	if got.ID != id {
		t.Fatalf("取到的消息 ID 不符: %q", got.ID)
	}
}

func TestDeleteMessageNotFound(t *testing.T) {
	svc, _, _, _, _ := newMsgFixture(t)

	err := svc.Delete(context.Background(), "u_1", "msg_01J8ZQ3K7N9P2V6R4T8W1Y5B3C")
	assertAppError(t, err, errs.CodeMessageNotFound, 404)
}

// ---- AppendAssistant（M3/M4 的落库入口）----

func TestAppendAssistantRejectsUnknownStatus(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	_, err := svc.AppendAssistant(context.Background(), "u_1", AppendAssistantInput{
		ConversationID: conv.ID,
		Content:        "半截回答",
		Status:         "weird",
	})
	assertAppError(t, err, errs.CodeInternalError, 500)
	if len(msgs.appended) != 0 {
		t.Fatal("状态非法时不得落库")
	}
}

func TestAppendAssistantDefaultsStatusToCompleted(t *testing.T) {
	svc, _, _, _, conv := newMsgFixture(t)

	got, err := svc.AppendAssistant(context.Background(), "u_1", AppendAssistantInput{
		ConversationID: conv.ID,
		Content:        "回答",
	})
	if err != nil {
		t.Fatalf("AppendAssistant 失败: %v", err)
	}
	if got.Status != MessageStatusCompleted {
		t.Errorf("缺省状态应为 completed，实际 %q", got.Status)
	}
	if got.Role != MessageRoleAssistant {
		t.Errorf("角色应为 assistant，实际 %q", got.Role)
	}
}

// TestAppendAssistantNormalizesEmptyJSONToNil 空的 references/tool_calls 要写 NULL。
//
// 写 `json.RawMessage("null")` 会被当成合法 JSON 存进列里，读出来是 `null`，
// 而契约里这两个字段是数组 —— 存 NULL 读出来才是「没有值」，由 DTO 输出 `[]`。
func TestAppendAssistantNormalizesEmptyJSONToNil(t *testing.T) {
	svc, _, msgs, _, conv := newMsgFixture(t)

	got, err := svc.AppendAssistant(context.Background(), "u_1", AppendAssistantInput{
		ConversationID: conv.ID,
		Content:        "回答",
	})
	if err != nil {
		t.Fatalf("AppendAssistant 失败: %v", err)
	}
	if got.References != nil || got.ToolCalls != nil {
		t.Fatalf("空值应归一化成 nil，实际 references=%q tool_calls=%q", got.References, got.ToolCalls)
	}
	if len(msgs.appended) != 1 {
		t.Fatalf("应落库 1 条，实际 %d", len(msgs.appended))
	}
}

// ---- 用量 ----

func TestMessageUsageIsZero(t *testing.T) {
	if !(*MessageUsage)(nil).IsZero() {
		t.Error("nil 用量应算空")
	}
	if !(&MessageUsage{}).IsZero() {
		t.Error("三个字段都是 0 应算空（上游会返回 usage 帧但字段缺省）")
	}
	if (&MessageUsage{TotalTokens: 1}).IsZero() {
		t.Error("有 token 就不算空")
	}
}
