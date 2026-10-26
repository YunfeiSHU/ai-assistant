package ids

import (
	"fmt"
	"sync"
	"testing"
)

// TestNewConcurrentUnique 是 B-14 的回归测试。
//
// ulid.Monotonic 的内部状态不是并发安全的：没有锁时，同一毫秒内的并发调用会生成
// 相同的 ULID，进而在 conversation 主键上撞车。这里用 2000 个并发调用把窗口放大。
func TestNewConcurrentUnique(t *testing.T) {
	const total = 2000

	results := make(chan string, total)
	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			results <- NewConversation()
		}()
	}
	wg.Wait()
	close(results)

	seen := make(map[string]struct{}, total)
	for id := range results {
		if !ValidatePrefix(id, PrefixConversation) {
			t.Fatalf("生成的 ID 不符合契约: %q", id)
		}
		if _, ok := seen[id]; ok {
			t.Fatalf("并发生成重复 ID: %s", id)
		}
		seen[id] = struct{}{}
	}

	if len(seen) != total {
		t.Fatalf("唯一 ID 数 = %d，期望 %d", len(seen), total)
	}
}

func ExampleNewConversation() {
	id := NewConversation()
	fmt.Println(ValidatePrefix(id, PrefixConversation))
	// Output: true
}
