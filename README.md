# AI Morning Briefing

每天早上 7:00 自动从 19+ 个 RSS 源抓取 AI 相关新闻，通过 LLM 分析后生成精美的 HTML 早报页面。

## 工作流程

```
RSS 源 (14 个) → 全文提取 → LLM 分析 → HTML 生成 → GitHub Pages 部署 → Telegram 通知
```

1. 从官方博客（OpenAI、Anthropic、Google AI、DeepMind）、媒体（The Verge、TechCrunch）、学术（ArXiv）等抓取 RSS
2. 多策略提取文章全文（article 标签、语义 class、JSON-LD、meta 标签）
3. LLM 分析每篇文章：AI 相关性过滤、重要性评分、核心要点提取
4. 生成暗色主题响应式 HTML 页面，支持搜索和分类筛选
5. 部署到 GitHub Pages 并发送 Telegram 通知

## 快速开始

```bash
# 手动运行
python3 fetch_news.py

# 跳过 LLM 分析（更快，无需 API）
python3 fetch_news.py --no-llm

# 运行并在浏览器中打开
python3 fetch_news.py --open
```

## 配置

编辑 `config.json` 自定义：
- **LLM 设置**：API 端点、模型、认证方式（支持任何 OpenAI 兼容 API）
- **RSS 源**：添加/删除源，每个源包含名称、URL、分类、权威度权重
- **通用设置**：每源最大条目数、文章时效、输出路径

## 定时任务

使用 macOS launchd 调度（`install_launchd.sh` 会自动适配当前路径）：

```bash
bash install_launchd.sh
```

## 敏感配置

Telegram token 等信息存放在外部文件中，不会进入版本控制：

```bash
mkdir -p ~/.config/ai-briefing
cat > ~/.config/ai-briefing/.env << 'EOF'
TG_BOT_TOKEN="your_bot_token"
TG_CHAT_ID="your_chat_id"
BRIEFING_URL="https://your-username.github.io/ai-morning-briefing/"
EOF
```

## 环境要求

- Python 3.9+（纯标准库，无需 pip 安装）
- macOS（用于 launchd 调度；Python 脚本在任何 OS 均可运行）
- OpenAI 兼容 LLM API（可选，`--no-llm` 跳过）

## 许可证

[MIT](LICENSE)
