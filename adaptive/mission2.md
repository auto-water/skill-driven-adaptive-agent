# Mission 2: Skill Lab & Reflection System

## 1. 核心目标
在已接入 MCP Playwright 的基础上，实现一套基于 **Plan-Execute-Reflect** 循环的 Skill 进化系统。让 Agent 能够将成功的漏洞验证轨迹总结为可复用的“技能包”。

## 2. Skill Lab 存储架构
- **根目录**: `/skill_db`
- **Skill 单元**: 每个 Skill 为一个独立文件夹，包含：
    - `skill.md`: 核心方法论，描述具体的漏洞利用步骤或绕过技巧。
    - `score.json`: 包含元数据（`id`, `description`, `keywords`, `ttl`, `success_count`）。

## 3. 核心逻辑重构
### A. Skill 检索与加载 (Execute 阶段)
- 在 `_chat_tool_agent_loop` 启动前，扫描 `skill_db` 中的所有 `score.json`。
- **关键词匹配**: 根据当前的 `user_prompt` 或 `plan.md` 提取关键词。
- **按需注入**: 若匹配成功，仅将对应的 `skill.md` 内容作为 `system` 消息注入提示词头部[cite: 1]。

### B. 经验萃取 (Reflect 阶段)
- 任务结束后（`run_continuously` 产生 `RunOutcome` 后），新增 `reflect_agent` 逻辑[cite: 1]。
- 分析执行日志和 `report.md`：
    - 若验证成功：将关键 Payload 和逻辑抽象为新 Skill，或更新现有 Skill 的评分。
    - 若验证失败：分析失败原因，不记录。

### C. TTL 新陈代谢机制
- **生命周期管理**: 
    - 每一轮完整的任务（Task）结束后，`skill_db` 中所有 Skill 的 `ttl` 减 1[cite: 1]。
    - 本轮被成功调用的 Skill，其 `ttl` 重置（或加法奖励）。
- **物理清理**: 扫描 `skill_db`，自动物理删除 `ttl <= 0` 的文件夹。

## 4. 约束条件
- **透明性**: 继承 `main.py` 的 `UsageTracker`，Reflect 阶段的 Token 消耗需单独分类统计[cite: 1]。
- **极简干预**: 仅在 `Execute` 阶段根据匹配度装载 Skill，禁止将整个 Skill 库一股脑塞入上下文。