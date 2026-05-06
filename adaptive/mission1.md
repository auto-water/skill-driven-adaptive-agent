# Mission 1: Seamless Playwright MCP Integration

## 1. 核心需求
在保持 `main.py` 现有 `_function_tools` (read_poc_file, validator_agent 等) 100% 可用的前提下，接入外部 Playwright MCP Server，使 Agent 具备浏览器自动化能力。

## 2. 技术实现路径
- **Client 初始化**: 在 `main.py` 中新增异步 MCP Client 逻辑，连接到 `mcp-server-playwright`。
- **混合工具路由 (Hybrid Dispatcher)**:
    - 修改 `execute_tool`：
        - 优先匹配 `_function_tools` 中的本地函数。
        - 若未命中，则通过 MCP Client 调用远程 Playwright 工具[cite: 1]。
- **动态 Schema 注入**:
    - 在 `_chat_tool_agent_loop` 中，将从 MCP 获取的 Playwright 工具定义 (JSON Schema) 合并到 `oai_tools` 列表中[cite: 1]。
- **数据兼容**: 所有的 Playwright 调用日志和 Token 消耗必须完整记录到现有的 `UsageTracker` 中[cite: 1]。

## 3. 约束条件 (Vibe Guardrails)
- **最小化改动**: 严禁改动任何与靶场验证相关的核心逻辑（如 `run_vulhub_case`）[cite: 1]。
- **容错处理**: 若 MCP Server 连接失败或调用超时，系统需捕获异常并允许 Agent 继续使用本地工具，不可导致主循环崩溃[cite: 1]。