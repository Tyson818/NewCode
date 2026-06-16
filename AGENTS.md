# MewCode

我正在构建一个终端 AI 编程助手（类似 Claude Code），项目名叫 MewCode，使用 Python 实现。

## 语言

中文回答，中文注释。

## 开发规则

开发前先阅读项目结构和相关代码，不要直接乱改。

修改代码时：

1. 优先保持现有项目结构
2. 不要修改无关文件
3. 不要随意新增依赖
4. 不要删除已有功能，除非明确说明原因

## 测试

开发完功能后，优先运行 Python 测试：

```bash
pytest
```

如果项目没有配置 pytest，则运行：

```bash
python -m unittest discover
```

如果本项目是终端应用，开发完功能后，用 tmux 做端到端测试：

1. 在 tmux 中启动 MewCode
2. 输入一段真实的对话请求
3. 观察 MewCode 是否正确调用工具、生成回复
4. 对照 checklist.md 逐项验收

如果当前环境不能使用 tmux，需要说明原因，并给出我可以手动执行的测试命令。

## 完成后回复

每次完成开发后，用中文说明：

1. 修改了什么
2. 运行了什么测试
3. 是否通过
4. 还有什么需要我确认



## LLM API 约定

本项目可能使用 Claude / Anthropic API 示例，但当前实际实现优先使用 OpenAI SDK + DeepSeek API。

实现要求：

- 使用 `openai` Python SDK
- 使用 OpenAI-compatible Chat Completions 风格
- API Key 从环境变量 `DEEPSEEK_API_KEY` 读取
- base_url 使用 `https://api.deepseek.com`
- 不要把 API Key 写死在代码里
- 遇到 Claude / Anthropic 示例代码时，转换为 OpenAI-compatible 写法
- 涉及最新 API 用法时，优先使用 Context7 查询文档
