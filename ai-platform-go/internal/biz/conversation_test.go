package biz

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/YunfeiSHU/ai-assistant/ai-platform-go/pkg/errs"
)

// ---- 自动标题（REQ-CONV-002 / docs/03-§3）----

func TestAutoTitle(t *testing.T) {
	// 兜底值里的日期按 UTC 取。用 +08:00 的 29 日 01:00（= UTC 28 日 17:00）
	// 来验证「本地日期」不会漏进来。
	utcDate := time.Date(2026, 9, 29, 1, 0, 0, 0, time.FixedZone("CST", 8*3600))

	cases := []struct {
		name    string
		content string
		max     int
		want    string
	}{
		{
			name:    "换行变空格而不是删除（AC-CONV-03）",
			content: "  你好\n\n世界  ",
			max:     30,
			want:    "你好 世界",
		},
		{
			name:    "制表符同样变空格",
			content: "第一段\t第二段",
			max:     30,
			want:    "第一段 第二段",
		},
		{
			name:    "去掉 Markdown 标记",
			content: "# 标题 **粗体** `代码` [1]",
			max:     30,
			want:    "标题 粗体 代码 1",
		},
		{
			name:    "连续空白折叠成一个空格",
			content: "a     b",
			max:     30,
			want:    "a b",
		},
		{
			name:    "超长按字符截断并补省略号",
			content: strings.Repeat("测", 50),
			max:     30,
			want:    strings.Repeat("测", 30) + AutoTitleEllipsis,
		},
		{
			name:    "恰好等于上限不截断",
			content: strings.Repeat("测", 30),
			max:     30,
			want:    strings.Repeat("测", 30),
		},
		{
			name:    "全是空白走兜底",
			content: " \n\t ",
			max:     30,
			want:    AutoTitleFallback + " " + utcDate.UTC().Format("01-02"),
		},
		{
			name:    "只剩 1 个字符走兜底（阈值是 2）",
			content: "好",
			max:     30,
			want:    AutoTitleFallback + " " + utcDate.UTC().Format("01-02"),
		},
		{
			name:    "2 个字符不算兜底",
			content: "你好",
			max:     30,
			want:    "你好",
		},
		{
			name:    "去掉标记后不足 2 字符也走兜底",
			content: "###",
			max:     30,
			want:    AutoTitleFallback + " " + utcDate.UTC().Format("01-02"),
		},
		{
			name:    "maxChars 非正数时用默认值 30",
			content: strings.Repeat("测", 40),
			max:     0,
			want:    strings.Repeat("测", AutoTitleMaxCharsDefault) + AutoTitleEllipsis,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := AutoTitle(tc.content, tc.max, utcDate); got != tc.want {
				t.Fatalf("AutoTitle(%q, %d) = %q，期望 %q", tc.content, tc.max, got, tc.want)
			}
		})
	}
}

// TestAutoTitlePreservesMultibyte 防止「按字节截断」把汉字切成非法 UTF-8。
func TestAutoTitlePreservesMultibyte(t *testing.T) {
	got := AutoTitle(strings.Repeat("测", 50), 30, testNow())
	if !strings.HasPrefix(got, strings.Repeat("测", 30)) {
		t.Fatalf("截断结果不是 30 个完整汉字: %q", got)
	}
	if strings.ContainsRune(got, '\uFFFD') {
		t.Fatalf("截断产生了非法 UTF-8: %q", got)
	}
}

// ---- 会话创建 / 查询 ----

func TestCreateConversationGeneratesIDAndDefaults(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	title := "  我的会话  "
	got, err := svc.Create(context.Background(), "u_1", CreateConversationInput{Title: &title})
	if err != nil {
		t.Fatalf("Create 失败: %v", err)
	}

	if !strings.HasPrefix(got.ID, "cv_") {
		t.Errorf("会话 ID 应以 cv_ 开头（REQ-CONV-001），实际 %q", got.ID)
	}
	if got.Title != "我的会话" {
		t.Errorf("标题应被 trim，实际 %q", got.Title)
	}
	if got.TitleSource != TitleSourceAuto {
		t.Errorf("新建会话的 title_source 应为 auto，实际 %q", got.TitleSource)
	}
	if got.Status != ConversationStatusActive {
		t.Errorf("新建会话应为 active，实际 %q", got.Status)
	}
	// 非 nil 是为了 JSON 输出 `[]` / `{}` 而不是 `null`（契约里默认值是空集合）。
	if got.KBIDs == nil || len(got.KBIDs) != 0 {
		t.Errorf("kb_ids 应为空切片而不是 nil，实际 %#v", got.KBIDs)
	}
	if got.Metadata == nil || len(got.Metadata) != 0 {
		t.Errorf("metadata 应为空 map 而不是 nil，实际 %#v", got.Metadata)
	}
	if !got.CreatedAt.Equal(testNow()) {
		t.Errorf("created_at 应使用注入时钟，实际 %v", got.CreatedAt)
	}
	if len(repo.created) != 1 {
		t.Fatalf("仓储应收到 1 次 Create，实际 %d", len(repo.created))
	}
}

// TestCreateConversationRejectsBlankModel 记录一条容易想当然的规则：
//
// Create 里没有「清空 model」的语义（缺省就是默认模型），但空串仍然被判为非法
// （与 PATCH 共用同一条校验）。刻意这么做的原因：同一个输入在两处接口给出不同结论
// 是客户端最容易踩的坑，而「create 时写空串想表达默认」本来就有两种合理读法。
func TestCreateConversationRejectsBlankModel(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	for _, raw := range []string{"", "   "} {
		model := raw
		_, err := svc.Create(context.Background(), "u_1", CreateConversationInput{Model: &model})
		assertFieldError(t, err, "model")
	}
	if len(repo.created) != 0 {
		t.Fatalf("校验失败时不该落库，实际 Create 了 %d 次", len(repo.created))
	}
}

func TestCreateConversationKeepsExplicitModel(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	model := "  deepseek-flash  "
	got, err := svc.Create(context.Background(), "u_1", CreateConversationInput{Model: &model})
	if err != nil {
		t.Fatalf("Create 失败: %v", err)
	}
	if got.Model == nil || *got.Model != "deepseek-flash" {
		t.Fatalf("显式 model 应被 trim 后原样保留，实际 %#v", got.Model)
	}
}

func TestCreateConversationValidation(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	longTitle := strings.Repeat("测", ConversationTitleMaxRunes+1)
	emptyModel := ""
	tooManyKB := make([]string, 0, ConversationKBIDsMax+1)
	for i := 0; i <= ConversationKBIDsMax; i++ {
		tooManyKB = append(tooManyKB, "kb_01J8ZQ3K7N9P2V6R4T8W1Y5B3C")
	}

	cases := []struct {
		name      string
		in        CreateConversationInput
		wantField string
	}{
		{"标题超长", CreateConversationInput{Title: &longTitle}, "title"},
		{"model 空串", CreateConversationInput{Model: &emptyModel}, "model"},
		{"kb_ids 超数量", CreateConversationInput{KBIDs: tooManyKB}, "kb_ids"},
		{"kb_ids 格式非法", CreateConversationInput{KBIDs: []string{"not-a-ulid"}}, "kb_ids"},
		{
			"metadata 键超长",
			CreateConversationInput{Metadata: map[string]string{
				strings.Repeat("k", ConversationMetadataKeyMax+1): "v",
			}},
			"metadata",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := svc.Create(context.Background(), "u_1", tc.in)
			assertFieldError(t, err, tc.wantField)
			if len(repo.created) != 0 {
				t.Fatalf("校验失败时不该调用仓储，实际 Create 了 %d 次", len(repo.created))
			}
		})
	}
}

// ---- 会话更新 ----

func TestUpdateConversationTitleMarksManual(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive, TitleSource: TitleSourceAuto}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	title := "  手工标题  "
	got, err := svc.Update(context.Background(), "u_1", "cv_1", UpdateConversationInput{Title: &title})
	if err != nil {
		t.Fatalf("Update 失败: %v", err)
	}

	if len(repo.updates) != 1 {
		t.Fatalf("仓储应收到 1 次 Update，实际 %d", len(repo.updates))
	}
	patch := repo.updates[0].Patch
	if patch.Title == nil || *patch.Title != "手工标题" {
		t.Errorf("补丁标题应为 trim 后的值，实际 %#v", patch.Title)
	}
	if patch.TitleSource == nil || *patch.TitleSource != TitleSourceManual {
		t.Fatalf("改标题必须同时把 title_source 置为 manual（AC-CONV-04），实际 %#v", patch.TitleSource)
	}
	if got.TitleSource != TitleSourceManual {
		t.Errorf("返回体里的 title_source 应为 manual，实际 %q", got.TitleSource)
	}
}

// TestUpdateConversationNoFieldsIsIdempotent 空补丁不该打库，也不该报错。
func TestUpdateConversationNoFieldsIsIdempotent(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive, Title: "已有标题"}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	got, err := svc.Update(context.Background(), "u_1", "cv_1", UpdateConversationInput{})
	if err != nil {
		t.Fatalf("空补丁应幂等成功，实际: %v", err)
	}
	if repo.updateCalls != 0 {
		t.Errorf("空补丁不该调用 Update，实际调用 %d 次", repo.updateCalls)
	}
	if got.Title != "已有标题" {
		t.Errorf("空补丁应回显当前状态，实际标题 %q", got.Title)
	}
}

func TestUpdateConversationModelNullClearsValue(t *testing.T) {
	model := "deepseek-v4-pro"
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive, Model: &model}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	// `{"model":null}` 是**有效**语义：回到全局默认模型，必须与「没传 model」区分。
	var in UpdateConversationInput
	if err := json.Unmarshal([]byte(`{"model":null}`), &in); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	if !in.Model.Present {
		t.Fatal("显式 null 必须让 Present = true，否则「清空 model」永远做不到")
	}
	if in.Model.Value != nil {
		t.Fatalf("显式 null 的 Value 应为 nil，实际 %v", *in.Model.Value)
	}

	got, err := svc.Update(context.Background(), "u_1", "cv_1", in)
	if err != nil {
		t.Fatalf("Update 失败: %v", err)
	}
	if got.Model != nil {
		t.Fatalf("model 应被清空，实际 %q", *got.Model)
	}
}

// TestPatchValueTriState 三态各自独立：未出现 / null / 有值。
func TestPatchValueTriState(t *testing.T) {
	var in UpdateConversationInput
	if err := json.Unmarshal([]byte(`{"model":"m1"}`), &in); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	if !in.Model.Present || in.Model.Value == nil || *in.Model.Value != "m1" {
		t.Fatalf("有值场景: Present=%v Value=%#v", in.Model.Present, in.Model.Value)
	}
	if in.KBIDs.Present {
		t.Fatal("未出现的字段 Present 必须为 false，否则「不动 kb_ids」会被当成「清空 kb_ids」")
	}
}

func TestUpdateConversationValidationRejectsEmptyModel(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	// 空串是非法值（要清空必须传 null）——这两者混起来是最常见的 API 歧义。
	var in UpdateConversationInput
	if err := json.Unmarshal([]byte(`{"model":""}`), &in); err != nil {
		t.Fatalf("反序列化失败: %v", err)
	}
	_, err := svc.Update(context.Background(), "u_1", "cv_1", in)
	assertFieldError(t, err, "model")
}

// ---- 归档 / 删除 ----

func TestSetArchivedWritesStatus(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	got, err := svc.SetArchived(context.Background(), "u_1", "cv_1", true)
	if err != nil {
		t.Fatalf("归档失败: %v", err)
	}
	if got.Status != ConversationStatusArchived {
		t.Errorf("归档后 status 应为 archived，实际 %q", got.Status)
	}

	got, err = svc.SetArchived(context.Background(), "u_1", "cv_1", false)
	if err != nil {
		t.Fatalf("取消归档失败: %v", err)
	}
	if got.Status != ConversationStatusActive {
		t.Errorf("取消归档后 status 应为 active，实际 %q", got.Status)
	}
}

// TestDeleteConversationNotFoundReturns404 删除别人的 id 必须 404 而不是 204：
// 204 会把「这个 id 存在」变成可探测的信息（docs/02-§7）。
func TestDeleteConversationNotFoundReturns404(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	err := svc.Delete(context.Background(), "u_other", "cv_1")
	assertAppError(t, err, errs.CodeConversationNotFound, 404)
	if conv.DeletedAt != nil {
		t.Fatal("越权删除不得真的删掉数据")
	}
}

func TestGetConversationCrossUserReturns404(t *testing.T) {
	conv := &Conversation{ID: "cv_1", UserID: "u_1", Status: ConversationStatusActive}
	repo := newFakeConvRepo(conv)
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	_, err := svc.Get(context.Background(), "u_other", "cv_1")
	assertAppError(t, err, errs.CodeConversationNotFound, 404)
}

// ---- 列表校验 ----

func TestListConversationsLimitOutOfRange(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	for _, limit := range []int{0, -1, PageLimitMax + 1} {
		_, err := svc.List(context.Background(), "u_1", ListConversationsInput{
			PaginationInput: PaginationInput{Limit: limit},
		})
		// REQ-AUTH-010：超上限必须报错，绝不能静默截断成 100。
		assertFieldError(t, err, "limit")
		if repo.listCalls != 0 {
			t.Fatalf("limit=%d 校验失败时不该调用仓储", limit)
		}
	}
}

func TestListConversationsRejectsUnknownStatus(t *testing.T) {
	repo := newFakeConvRepo()
	svc := NewConversationService(ConversationDeps{Conversations: repo, Clock: testNow})

	_, err := svc.List(context.Background(), "u_1", ListConversationsInput{
		PaginationInput: PaginationInput{Limit: 20},
		Status:          "deleted",
	})
	assertFieldError(t, err, "status")
}

func TestListConversationsNilResultNormalized(t *testing.T) {
	svc := NewConversationService(ConversationDeps{Conversations: &nilListConvRepo{}, Clock: testNow})

	got, err := svc.List(context.Background(), "u_1", ListConversationsInput{
		PaginationInput: PaginationInput{Limit: 20},
	})
	if err != nil {
		t.Fatalf("List 失败: %v", err)
	}
	if got == nil {
		t.Fatal("仓储返回 nil 时服务层应归一化成空列表（否则 handler 会空指针）")
	}
	// Items 允许为 nil：**输出**的 [] 由 service 层（NewPage / ToConversationResponses）
	// 保证，biz 不重复做一次同样的归一化。
	if got.HasMore {
		t.Error("空结果不该有 has_more")
	}
}

// nilListConvRepo 返回 (nil, nil)，模拟仓储「没数据也没错误」的边界。
type nilListConvRepo struct{}

func (r *nilListConvRepo) List(context.Context, string, ListConversationsInput) (*ConversationList, error) {
	return nil, nil
}

func (r *nilListConvRepo) Create(context.Context, *Conversation) error { return nil }
func (r *nilListConvRepo) GetOwned(_ context.Context, _, id string) (*Conversation, error) {
	return &Conversation{ID: id, UserID: "u_1", Status: ConversationStatusActive}, nil
}
func (r *nilListConvRepo) Update(context.Context, string, string, ConversationPatch, time.Time) error {
	return nil
}
func (r *nilListConvRepo) SetAutoTitle(context.Context, string, string, string, time.Time) (bool, error) {
	return false, nil
}
func (r *nilListConvRepo) SoftDelete(context.Context, string, string, time.Time) error { return nil }

// ---- 断言工具 ----

// assertAppError 断言错误是给定错误码的 AppError。
func assertAppError(t *testing.T, err error, code errs.Code, status int) {
	t.Helper()
	if err == nil {
		t.Fatalf("期望错误 %s，实际 nil", code)
	}
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("期望 *errs.AppError，实际 %T: %v", err, err)
	}
	if appErr.Code() != code {
		t.Fatalf("错误码期望 %s，实际 %s（%v）", code, appErr.Code(), err)
	}
	if appErr.Status() != status {
		t.Fatalf("HTTP 状态期望 %d，实际 %d", status, appErr.Status())
	}
}

// assertFieldError 断言错误是 400 INVALID_ARGUMENT 且 details.fields 里含指定字段。
func assertFieldError(t *testing.T, err error, want string) {
	t.Helper()
	if err == nil {
		t.Fatalf("期望 400 INVALID_ARGUMENT（字段 %s），实际 nil", want)
	}
	appErr, ok := errs.As(err)
	if !ok {
		t.Fatalf("期望 *errs.AppError，实际 %T: %v", err, err)
	}
	if appErr.Code() != errs.CodeInvalidArgument {
		t.Fatalf("错误码期望 INVALID_ARGUMENT，实际 %s（%v）", appErr.Code(), err)
	}
	if !hasFieldError(appErr, want) {
		t.Fatalf("details.fields 里应包含 field=%q，实际 %#v", want, appErr.Details()["fields"])
	}
}

func hasFieldError(appErr *errs.AppError, field string) bool {
	raw, ok := appErr.Details()["fields"].([]map[string]any)
	if !ok {
		return false
	}
	for _, item := range raw {
		if item["field"] == field {
			return true
		}
	}
	return false
}
