package cryptox

import (
	"fmt"
	"testing"
	"time"
)

// 密码派生的成本是登录/注册链路上最大的一块固定开销，且不是慢在 IO 上，
// 所以从日志的 elapsed_ms 里只能看到现象、看不到原因。这个基准把
// 「一次 Argon2id 到底花多少毫秒」量出来，供 docs/09 引用。
//
// 关键点是 `BurnPasswordHash`：用户不存在时也要跑一遍等价的派生 —— 这就是
// 「登录失败（401）耗时与成功（200）同量级」的原因，它是防时序枚举的代价，
// 不是缺陷（见 internal/biz/auth.go）。
func BenchmarkHashPasswordDefault(b *testing.B) {
	p := DefaultArgon2Params()
	for i := 0; i < b.N; i++ {
		if _, err := HashPassword("correct horse battery staple", p); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkVerifyPasswordDefault(b *testing.B) {
	p := DefaultArgon2Params()
	enc, err := HashPassword("correct horse battery staple", p)
	if err != nil {
		b.Fatal(err)
	}
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := VerifyPassword("correct horse battery staple", enc); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkBurnPasswordHash(b *testing.B) {
	for i := 0; i < b.N; i++ {
		BurnPasswordHash("some-submitted-password")
	}
}

// TestReportPasswordCost 打印绝对值（benchmark 只给 ns/op，看数字不如直接看毫秒）。
// 输出里三个数字应当同量级 —— 这正是「401 也不快」的证据。
func TestReportPasswordCost(t *testing.T) {
	p := DefaultArgon2Params()
	enc, err := HashPassword("correct horse battery staple", p)
	if err != nil {
		t.Fatal(err)
	}

	measure := func(name string, fn func()) time.Duration {
		const runs = 5
		start := time.Now()
		for i := 0; i < runs; i++ {
			fn()
		}
		d := time.Since(start) / runs
		t.Logf("%-16s 平均 %8.1f ms", name, float64(d.Microseconds())/1000)
		return d
	}

	measure("HashPassword", func() { _, _ = HashPassword("correct horse battery staple", p) })
	measure("VerifyPassword", func() { _, _ = VerifyPassword("correct horse battery staple", enc) })
	measure("BurnPasswordHash", func() { BurnPasswordHash("some-submitted-password") })

	t.Logf("参数 m=%d KiB t=%d p=%d", p.Memory, p.Iterations, p.Parallelism)

	// 内存维度扫描：Argon2id 是 memory-hard（每个 lane 都要实际触碰 m/p 的内存），
	// 耗时应当随 m 近似线性增长。增长远陡于线性说明瓶颈是内存压力而非算力
	// （64MB×4 lane 会触发硬缺页），这组数字用来判断「慢是参数选的，还是机器被压着」。
	for _, mb := range []int{8, 32, 64, 128} {
		params := Argon2Params{Memory: uint32(mb) * 1024, Iterations: p.Iterations, Parallelism: p.Parallelism, SaltLen: 16, KeyLen: 32}
		d := measure(fmt.Sprintf("m=%3dMiB", mb), func() {
			_, _ = HashPassword("correct horse battery staple", params)
		})
		t.Logf("   → 每 MiB %5.2f ms", float64(d.Microseconds())/1000/float64(mb))
	}
}
