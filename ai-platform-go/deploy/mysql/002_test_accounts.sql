-- =============================================================================
-- go-services（网关）· 分阶段测试账号
--
-- 目的：每个里程碑（M1~M6）有**专属的测试账号**，使 curl 验收脚本
--       (1) 互不污染（会话/消息/配额都是按 user_id 隔离的）；
--       (2) 可重复执行（本脚本每次跑都把账号重置回已知状态）；
--       (3) 不依赖「先调注册接口」——注册接口本身也是被验收对象，
--           用它来准备数据会让验收循环依赖。
--
-- 幂等：INSERT ... ON DUPLICATE KEY UPDATE，可重复执行。
--       **不含任何 DROP / TRUNCATE**，不会碰用户的真实数据。
--
-- 执行：
--   mysql --default-character-set=utf8mb4 -u root -p -e "source 002_test_accounts.sql"
--
-- ⚠️ 本文件只用于开发/验收环境。生产库 MUST NOT 执行。
-- =============================================================================

SET NAMES utf8mb4;

USE `ai_platform`;


-- -----------------------------------------------------------------------------
-- 阶段 → 账号对照表
-- -----------------------------------------------------------------------------
--   M1 鉴权 / 用户        u_test_stage1   stage1@test.local    Stage1#Test2026   free
--   M2 会话 / 消息        u_test_stage2   stage2@test.local    Stage2#Test2026   free
--   M3 编排（非流式）      u_test_stage3   stage3@test.local    Stage3#Test2026   free
--   M4 编排（流式 SSE）    u_test_stage4   stage4@test.local    Stage4#Test2026   free
--   M5 配额 / 限流 / 上传  u_test_stage5   stage5@test.local    Stage5#Test2026   free
--   M6 可观测 / 审计      u_test_stage6   stage6@test.local    Stage6#Test2026   free
--   跨阶段：配额对照        u_test_pro     pro@test.local       Pro#Test2026      pro
--   跨阶段：禁用账号        u_test_disabled disabled@test.local Disabled#Test2026 free
--   跨阶段：过期令牌        u_test_expired expired@test.local  Expired#Test2026  free
--
-- 为什么密码都带 `#` 与年份后缀：既有大小写、数字、符号（能通过强度校验），
-- 又不会出现在常见弱密码表里 —— 否则 `too_common` 分支会永远命中，
-- 反而掩盖了「正常密码能注册成功」这条验收项。


-- -----------------------------------------------------------------------------
-- 测试账号
-- -----------------------------------------------------------------------------
-- 密码哈希用 `go run ./cmd/hashpw -passwords "..."` 离线生成（argon2id,
-- m=65536,t=3,p=4，与 conf 的默认参数一致）。
-- 直接写哈希而不是明文：网关只存哈希，脚本里出现明文会让人误以为库里存明文。
--
-- ON DUPLICATE KEY UPDATE 的语义：**每次执行都把测试账号拉回初始状态**，
-- 包括把 token_version 重置为 1 —— 这会让上一轮跑出来的 access token 全部失效，
-- 保证每轮验收都从「登录拿新令牌」开始，不会因为残留令牌而掩盖鉴权缺陷。
INSERT INTO `user`
  (`id`, `email`, `nickname`, `password_hash`, `plan`, `status`, `token_version`, `created_at`, `updated_at`)
VALUES
  ('u_test_stage1',   'stage1@test.local',   'M1 鉴权测试', '$argon2id$v=19$m=65536,t=3,p=4$ny+Impg44TAL4pOFzYjVlA$L7CdeVfw75XmRFCQIvjWzYauKiOQo9ywDTeJY++NJQs', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_stage2',   'stage2@test.local',   'M2 会话测试', '$argon2id$v=19$m=65536,t=3,p=4$bofCkUNJHvOCbtctrSQw5g$JsdCQ9cLb6Gtdz0OFdH+em9x451FdSFstBuWt7DJpjE', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_stage3',   'stage3@test.local',   'M3 编排测试', '$argon2id$v=19$m=65536,t=3,p=4$S8EWE3diQN+BnF4B+iE+sA$AP4Wy/zWp551ELvNYQc26B7I1BN5nRn0j/2VdT3c0tQ', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_stage4',   'stage4@test.local',   'M4 流式测试', '$argon2id$v=19$m=65536,t=3,p=4$X7LUX8Icj85qUvoFDrF65A$zKjt2z6SshDiBAW8r4xjAn8Nif2ZZrMOqRUcJrzjdsQ', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_stage5',   'stage5@test.local',   'M5 配额测试', '$argon2id$v=19$m=65536,t=3,p=4$BTeNGP9D2eOrWaazxh+cVg$/Ly3zUKMifA16VmvoMzLj5UfymbFF4BA/tKgSxo2sXA', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_stage6',   'stage6@test.local',   'M6 观测测试', '$argon2id$v=19$m=65536,t=3,p=4$pHalc8krzCsxP+jpUmmwyQ$Uck9Q4cT8r8oxDujvJyCRdg1KOxsHutg5h77mBLS2Yo', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_pro',      'pro@test.local',      'Pro 对照账号', '$argon2id$v=19$m=65536,t=3,p=4$EBGWpVJP+F/kAZ6NXYMohw$D1Y047dd0ZtFYNI+ju9GavtgayMVUlOPbew3mPKGw4M', 'pro',  'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_disabled', 'disabled@test.local', '禁用账号',    '$argon2id$v=19$m=65536,t=3,p=4$e1Mt2LSd6Zndqxp5HJ3RwQ$tT9knVJs9oLAvncMBmvtlURqQBuyrjrrE1HU7Q2j8dw', 'free', 'disabled', 1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)),
  ('u_test_expired',  'expired@test.local',  '过期令牌账号', '$argon2id$v=19$m=65536,t=3,p=4$ZyHcGyDMbZ2Yp8pHQgZetw$FW5OeC1i7mENH0wDMLW0K7b7c9ULRvZaVsYMM/bGmPc', 'free', 'active',   1, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3))
ON DUPLICATE KEY UPDATE
  `nickname`      = VALUES(`nickname`),
  `password_hash` = VALUES(`password_hash`),
  `plan`          = VALUES(`plan`),
  `status`        = VALUES(`status`),
  `token_version` = 1,
  `last_login_at` = NULL,
  `updated_at`    = UTC_TIMESTAMP(3);


-- -----------------------------------------------------------------------------
-- 清理上一轮验收留下的刷新令牌与配额计数
-- -----------------------------------------------------------------------------
-- 只删 `u_test_%`，绝不碰真实用户。
-- 不清 conversation / message：那是验收时要人工观察的内容，
-- 而且 M2 的游标分页用例本身就需要多页数据；需要干净环境时用
-- 各阶段 curl 脚本末尾的清理段，或手动指定 user_id 删除。
DELETE FROM `refresh_token` WHERE `user_id` LIKE 'u\_test\_%';
DELETE FROM `quota_usage`   WHERE `user_id` LIKE 'u\_test\_%';
DELETE FROM `usage_record`  WHERE `user_id` LIKE 'u\_test\_%';


-- -----------------------------------------------------------------------------
-- 自检
-- -----------------------------------------------------------------------------
-- 期望 9 行；`status` 应有 1 行为 disabled，`plan` 应有 1 行为 pro。
SELECT `id`, `email`, `plan`, `status`, `token_version`
  FROM `user`
 WHERE `id` LIKE 'u\_test\_%'
 ORDER BY `id`;
