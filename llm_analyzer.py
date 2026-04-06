#!/usr/bin/env python3
"""
LLM 分析模块 - 使用 OpenAI 兼容 API 进行文章摘要、Toulmin 论证分析和翻译。

支持认证方式：
  1. API Key: Authorization: Bearer <api_key>
  2. OAuth Token: Authorization: Bearer <oauth_token>  (OpenClaw / Codex 等)
  3. 自定义 Header: 任意 header 名称和值

用法：
    analyzer = LLMAnalyzer(
        base_url="https://api.openai.com/v1",
        api_key="sk-...",
        model="gpt-4o-mini",
    )
    result = analyzer.analyze_article(title, summary, full_text, source_name)
"""

import json
import ssl
import urllib.request
import urllib.error
import concurrent.futures
import time
import sys
import re
from typing import Optional, Dict, Any, List


# ---------------------------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是一位资深 AI 行业分析师。你的任务是将新闻文章转化为高质量的结构化摘要卡片。

## 第一步：判断相关性

首先判断这篇文章是否与 AI / 人工智能 / 机器学习 / 大模型 / 深度学习 / 数据科学相关。
如果完全无关（如纯粹的政治、体育、娱乐、与 AI 无关的普通科技新闻），请返回：
```json
{"ai_relevant": false}
```

## 第二步：如果相关，返回完整分析

请严格按以下 JSON 格式返回（不要包含任何其他文字）：

```json
{
  "ai_relevant": true,
  "summary": "一句话概要，说清楚发生了什么，不超过100个中文字",
  "why_it_matters": "用大白话说这意味着什么，1-2句话",
  "key_details": [
    "文章核心要点1",
    "文章核心要点2",
    "文章核心要点3",
    "文章核心要点4"
  ],
  "background": "背景脉络：150-200字，这件事的前因后果，帮读者理解完整来龙去脉",
  "deep_analysis": "深度解读：150-250字，基于文章内容的深层分析，提炼核心信息和深层含义",
  "importance": 4,
  "is_follow_up": false,
  "categories": ["大模型发布", "开源生态"],
  "source_type": "news",
  "reading_minutes": 3
}
```

## 字段规则

1. **summary** — 一句话概要，客观陈述事实即可，不需要强行提炼"论点"
   - ✅ "OpenAI 发布 GPT-5，在数学推理基准上比 GPT-4 提升约 18%"
   - ✅ "欧盟 AI 法案正式生效，高风险 AI 系统需在 2026 年前完成合规"
   - ❌ 过于笼统："OpenAI 发布了新模型"
   - 不超过 100 个中文字

2. **why_it_matters** — 用简单直白的话解释：这件事对读者意味着什么？不要用行业黑话，像给朋友讲新闻一样说清楚"所以呢"。1-2句话。

3. **key_details** — 3-5 个文章中的核心要点。不限于数据，可以是关键结论、重要人物观点、技术要点、政策细节、时间节点等——只要是文章想传递的重要信息就应该提取出来。每条不超过80个中文字。

4. **background** — 背景脉络（150-200字）：帮不了解前情的读者快速建立上下文。这件事是怎么发展到现在的？之前发生了什么关键事件？涉及哪些重要角色？把前因后果讲清楚，让读者有完整的来龙去脉。

5. **deep_analysis** — 深度解读（150-250字）：基于文章内容，提炼出文章想传递的深层信息。可以是技术本质、商业逻辑、战略意图、行业趋势、对不同群体的具体影响等。重点是对文章信息的总结和提炼，不要脱离原文自由发挥。要有信息量，让读者读完觉得"比只看标题多了解了很多"。

6. **importance** — 重要性评分 1-5：
   - 5: 行业格局级事件（重大产品发布、突破性论文、重大政策）
   - 4: 显著进展（重要更新、有影响力的研究、大额融资）
   - 3: 值得关注（常规更新、行业动态、中等规模事件）
   - 2: 一般资讯（小更新、边缘话题）
   - 1: 低价值（内容不足、重复信息、软文）

7. **is_follow_up** — 布尔值，判断此文是否属于"旧事新炒"：
   - true: 文章讨论的是一个已被广泛报道的事件，且没有提供实质性新信息（仅换角度、换媒体重写、补充评论但无新事实）
   - false: 文章报道的是新事件，或虽是已有事件的后续但包含重要新进展（新数据、新政策反应、重大更新等）
   - 标记为 true 的文章会在排序中被降权

8. **categories** — 1-2 个分类标签，从以下选取：
   大模型发布 | 开源生态 | AI 政策监管 | 芯片与算力 | 产品与应用 |
   安全与对齐 | 融资与商业 | 学术研究 | AI 工具 | 具身智能 |
   自动驾驶 | AI 医疗 | AI 编程 | 行业观点 | 其他

8. **source_type** — 来源类型：
   "paper"（学术论文）| "news"（新闻媒体）| "official"（官方博客）|
   "opinion"（观点文章）| "community"（社区讨论）| "video"（视频）

9. **reading_minutes** — 预估原文阅读时间（分钟），基于内容深度和长度，整数 1-30

**注意**：is_follow_up 要认真判断。只有文章确实没有任何新事实、新数据、纯属旧事换角度重写时才标 true。有新进展的后续报道应标 false。

## 关键指令
- 所有输出必须是中文（如果原文是英文，翻译为地道的中文）
- 【最重要】直接输出 JSON，第一个字符必须是 `{`，最后一个字符必须是 `}`。不要输出任何思考过程、解释、markdown代码块标记或其他非JSON文字
- 如果文章内容太少无法充分分析，仍然返回完整 JSON 结构，importance 设为 1
- summary 和 why_it_matters 的语言要简洁有力、通俗易懂，像给朋友讲新闻一样
- key_details 要尽量多提取文章核心要点，把文章最重要的信息都涵盖进去
- background 和 deep_analysis 每个字段控制在 150 字以内，宁可精炼也不要冗长"""

DIGEST_SYSTEM_PROMPT = """你是一位资深 AI 行业主编。你的任务是从今天的所有新闻摘要中，提炼出一段编辑导语。

请严格按以下 JSON 格式返回（不要包含任何其他文字）：

```json
{
  "editorial": "150-200字的编辑导语，概述今天 AI 领域的整体动态、关键主题和值得思考的趋势"
}
```

## 规则
- editorial 要有洞察力，不是简单罗列新闻标题，而是点出今天的"主旋律"
- 串联不同新闻之间的关联，帮助读者看到全局图景
- 可以提出一个值得思考的问题收尾
- 语言简洁有力，像《经济学人》或《晚点LatePost》的编辑风格
- 只返回 JSON，不要任何额外说明文字"""

DIGEST_USER_TEMPLATE = """以下是今天的 {count} 条 AI 新闻摘要，请提炼今日速览：

{summaries}"""

USER_PROMPT_TEMPLATE = """分析以下文章：

【标题】{title}

【来源】{source_name}

【摘要】{summary}

【正文】{full_text}"""


# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

class LLMAnalyzer:
    """OpenAI 兼容 API 的 LLM 分析器。"""

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        auth_type: str = "bearer",       # "bearer" | "custom"
        auth_header: str = "Authorization",  # 自定义 header 名
        auth_prefix: str = "Bearer",      # header 值前缀
        max_retries: int = 3,
        timeout: int = 60,
        max_workers: int = 4,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.auth_type = auth_type
        self.auth_header = auth_header
        self.auth_prefix = auth_prefix
        self.max_retries = max_retries
        self.timeout = timeout
        self.max_workers = max_workers
        self.temperature = temperature
        self.max_tokens = max_tokens

        # SSL context（某些服务需要跳过验证）
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------
    # 底层 API 调用
    # ------------------------------------------------------------------

    def _build_headers(self) -> dict:
        """构建请求头，支持多种认证方式。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            if self.auth_type == "bearer":
                headers["Authorization"] = f"Bearer {self.api_key}"
            elif self.auth_type == "custom":
                value = f"{self.auth_prefix} {self.api_key}" if self.auth_prefix else self.api_key
                headers[self.auth_header] = value
        return headers

    def _call_api(self, messages: List[Dict[str, str]]) -> str:
        """调用 chat completions API，带重试。"""
        url = f"{self.base_url}/chat/completions"
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }).encode("utf-8")
        headers = self._build_headers()

        last_error = None
        for attempt in range(self.max_retries):
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
                opener = urllib.request.build_opener(
                    urllib.request.HTTPSHandler(context=self._ssl_ctx)
                )
                with opener.open(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    return body["choices"][0]["message"]["content"]

            except urllib.error.HTTPError as e:
                last_error = e
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8", errors="replace")[:500]
                except:
                    pass
                # 429 / 5xx 可重试
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries - 1:
                    wait = min(2 ** attempt * 2, 30)
                    print(f"  ⏳ HTTP {e.code}, {wait}s 后重试... ({error_body[:100]})")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"HTTP {e.code}: {error_body}")

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = min(2 ** attempt * 2, 30)
                    print(f"  ⏳ 网络错误, {wait}s 后重试... ({e})")
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"网络错误: {e}")

        raise RuntimeError(f"重试 {self.max_retries} 次后仍失败: {last_error}")

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> dict:
        """从 LLM 响应中提取 JSON，兼容各种包裹和截断情况。"""
        text = text.strip()

        # 去掉所有 markdown 代码块标记
        text = re.sub(r'```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```', '', text)
        text = text.strip()

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 找到第一个 {
        brace_start = text.find('{')
        if brace_start == -1:
            return {}

        # 取从第一个 { 到末尾的所有内容
        fragment = text[brace_start:]

        # 先找最后一个 }，尝试完整解析
        brace_end = fragment.rfind('}')
        if brace_end > 0:
            try:
                return json.loads(fragment[:brace_end + 1])
            except json.JSONDecodeError:
                pass

        # JSON 被截断了，尝试修复
        # 策略：逐步裁剪尾部残缺内容，然后补齐括号
        truncated = fragment.rstrip()
        for _ in range(10):
            # 关闭未闭合的字符串（找最后一个未配对的引号）
            # 计算引号数，如果奇数则在末尾补一个
            if truncated.count('"') % 2 == 1:
                truncated += '"'

            # 移除尾部不完整的键值对
            # 如: ..."key": "val   或  ..."key": [..."item
            truncated = re.sub(r',\s*"[^"]*"\s*:\s*"[^"]*$', '', truncated)
            truncated = re.sub(r',\s*"[^"]*"\s*:?\s*$', '', truncated)
            truncated = re.sub(r',\s*"[^"]*$', '', truncated)
            truncated = truncated.rstrip(', \n\r\t')

            # 补齐缺少的闭合括号
            open_braces = truncated.count('{') - truncated.count('}')
            open_brackets = truncated.count('[') - truncated.count(']')
            fixed = truncated + ']' * max(0, open_brackets) + '}' * max(0, open_braces)

            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                # 如果还是失败，尝试更激进地裁剪最后一个逗号之后的内容
                last_comma = truncated.rfind(',')
                if last_comma > 0:
                    truncated = truncated[:last_comma]
                else:
                    break

        return {}

    @staticmethod
    def _validate_result(data: dict) -> dict:
        """校验和修正 LLM 返回的结构。"""
        # 检查 AI 相关性
        if not data.get("ai_relevant", True):
            return {"ai_relevant": False}

        result = {
            "ai_relevant": True,
            "summary": str(data.get("summary", "无法提取摘要"))[:100],
            "why_it_matters": str(data.get("why_it_matters", ""))[:200],
            "key_details": [],
            "background": str(data.get("background", ""))[:600],
            "deep_analysis": str(data.get("deep_analysis", ""))[:800],
            "importance": 1,
            "categories": [],
            "source_type": str(data.get("source_type", "news")),
            "reading_minutes": 1,
        }

        # importance
        try:
            imp = int(data.get("importance", 1))
            result["importance"] = max(1, min(5, imp))
        except (TypeError, ValueError):
            result["importance"] = 1

        # reading_minutes
        try:
            rm = int(data.get("reading_minutes", 1))
            result["reading_minutes"] = max(1, min(30, rm))
        except (TypeError, ValueError):
            result["reading_minutes"] = 1

        # key_details
        raw_details = data.get("key_details", [])
        if isinstance(raw_details, list):
            for d in raw_details[:5]:
                if isinstance(d, str) and d.strip():
                    result["key_details"].append(d.strip()[:80])
                elif isinstance(d, dict):
                    result["key_details"].append(str(d.get("text", ""))[:80])

        # categories
        raw_cats = data.get("categories", ["其他"])
        if isinstance(raw_cats, list):
            result["categories"] = [str(c) for c in raw_cats[:2]]
        else:
            result["categories"] = ["其他"]

        # source_type 校验
        valid_types = {"paper", "news", "official", "opinion", "community", "video"}
        if result["source_type"] not in valid_types:
            result["source_type"] = "news"

        return result

    # ------------------------------------------------------------------
    # 核心分析方法
    # ------------------------------------------------------------------

    def analyze_article(
        self,
        title: str,
        summary: str = "",
        full_text: str = "",
        source_name: str = "",
    ) -> dict:
        """
        分析单篇文章，返回 Toulmin 结构化数据（中文）。

        Returns:
            dict with keys: claim, grounds, warrant, confidence,
                           rebuttal, categories, source_type
        """
        # 截断过长文本，节省 token
        if full_text and len(full_text) > 3000:
            full_text = full_text[:3000] + "…（已截断）"
        if summary and len(summary) > 800:
            summary = summary[:800] + "…"

        user_msg = USER_PROMPT_TEMPLATE.format(
            title=title or "无标题",
            source_name=source_name or "未知",
            summary=summary or "无摘要",
            full_text=full_text or "无正文",
        )

        try:
            response = self._call_api([
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            data = self._extract_json(response)
            if not data:
                print(f"  ⚠️ JSON 解析失败: {response[:200]}")
                return self._fallback(title)
            return self._validate_result(data)

        except RuntimeError as e:
            print(f"  ❌ LLM 分析失败 [{title[:30]}]: {e}")
            return self._fallback(title)

    @staticmethod
    def _fallback(title: str) -> dict:
        """LLM 调用失败时的兜底结果。"""
        return {
            "ai_relevant": True,
            "summary": title[:100] if title else "无法获取分析",
            "why_it_matters": "",
            "key_details": [],
            "background": "",
            "deep_analysis": "",
            "importance": 1,
            "categories": ["其他"],
            "source_type": "news",
            "reading_minutes": 1,
        }

    # ------------------------------------------------------------------
    # 今日速览（全局综合）
    # ------------------------------------------------------------------

    def generate_digest(self, analyses: List[dict]) -> dict:
        """
        从所有文章分析中生成"今日 3 分钟速览"。

        Args:
            analyses: 带 analysis 字段的文章列表

        Returns:
            dict with keys: editorial, top_stories
        """
        # 构建摘要列表供 LLM 综合
        summaries_text = ""
        for i, item in enumerate(analyses):
            a = item.get("analysis", {})
            if not a.get("ai_relevant", True):
                continue
            summary = a.get("summary", item.get("title", ""))
            importance = a.get("importance", 1)
            source = item.get("source_name", "")
            summaries_text += f"[{i}] ({source}, 重要性{importance}) {summary}\n"

        if not summaries_text.strip():
            return {"editorial": "今天暂无重要 AI 新闻。", "top_stories": []}

        user_msg = DIGEST_USER_TEMPLATE.format(
            count=len([a for a in analyses if a.get("analysis", {}).get("ai_relevant", True)]),
            summaries=summaries_text,
        )

        try:
            response = self._call_api([
                {"role": "system", "content": DIGEST_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ])
            data = self._extract_json(response)
            if not data:
                return {"editorial": "速览生成失败。", "top_stories": []}

            # 校验
            result = {
                "editorial": str(data.get("editorial", ""))[:300],
                "top_stories": [],
            }
            for s in data.get("top_stories", [])[:5]:
                if isinstance(s, dict):
                    result["top_stories"].append({
                        "index": int(s.get("index", 0)),
                        "headline": str(s.get("headline", ""))[:30],
                        "why": str(s.get("why", ""))[:80],
                    })
            return result

        except Exception as e:
            print(f"  ❌ 速览生成失败: {e}")
            return {"editorial": "速览生成失败。", "top_stories": []}

    # ------------------------------------------------------------------
    # 批量分析
    # ------------------------------------------------------------------

    def batch_analyze(
        self,
        articles: List[dict],
        show_progress: bool = True,
    ) -> List[dict]:
        """
        批量分析文章列表。

        Args:
            articles: 每个 dict 需含 title, summary, full_text, source_name
            show_progress: 是否打印进度

        Returns:
            与 articles 等长的分析结果列表
        """
        total = len(articles)
        if total == 0:
            return []

        print(f"\n🧠 开始 LLM 分析（共 {total} 篇，并发 {self.max_workers}）...")

        # 保持顺序的结果列表
        results = [None] * total
        completed = [0]  # 用列表实现闭包可变

        def _worker(idx: int) -> tuple:
            a = articles[idx]
            result = self.analyze_article(
                title=a.get("title", ""),
                summary=a.get("summary", ""),
                full_text=a.get("article_text", a.get("full_text", "")),
                source_name=a.get("source_name", ""),
            )
            return idx, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_worker, i): i for i in range(total)}
            for future in concurrent.futures.as_completed(futures):
                try:
                    idx, result = future.result()
                    results[idx] = result
                except Exception as e:
                    idx = futures[future]
                    results[idx] = self._fallback(articles[idx].get("title", ""))
                    print(f"  ❌ 第 {idx+1} 篇处理异常: {e}")

                completed[0] += 1
                if show_progress:
                    print(f"  ✅ [{completed[0]}/{total}] {articles[futures[future]].get('title', '')[:40]}")

        success = sum(1 for r in results if r and r.get("importance", 0) > 1)
        print(f"\n📊 分析完成：{success}/{total} 篇获得有效分析")

        return results


# ---------------------------------------------------------------------------
# 工具函数：从 config 创建 analyzer
# ---------------------------------------------------------------------------

def create_analyzer_from_config(config: dict) -> Optional[LLMAnalyzer]:
    """从 config.json 中的 llm 配置创建 LLMAnalyzer 实例。"""
    llm_cfg = config.get("llm")
    if not llm_cfg or not llm_cfg.get("enabled", False):
        print("  ℹ️ LLM 分析未启用（config.llm.enabled = false）")
        return None

    return LLMAnalyzer(
        base_url=llm_cfg.get("base_url", "https://api.openai.com/v1"),
        api_key=llm_cfg.get("api_key", ""),
        model=llm_cfg.get("model", "gpt-4o-mini"),
        auth_type=llm_cfg.get("auth_type", "bearer"),
        auth_header=llm_cfg.get("auth_header", "Authorization"),
        auth_prefix=llm_cfg.get("auth_prefix", "Bearer"),
        max_retries=llm_cfg.get("max_retries", 3),
        timeout=llm_cfg.get("timeout", 60),
        max_workers=llm_cfg.get("max_workers", 4),
        temperature=llm_cfg.get("temperature", 0.3),
        max_tokens=llm_cfg.get("max_tokens", 1500),
    )


# ---------------------------------------------------------------------------
# 测试入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os

    # 从环境变量读取配置
    analyzer = LLMAnalyzer(
        base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("LLM_API_KEY", ""),
        model=os.environ.get("LLM_MODEL", "gpt-4o-mini"),
    )

    test_article = {
        "title": "OpenAI announces GPT-5 with breakthrough reasoning capabilities",
        "summary": "OpenAI has released GPT-5, claiming significant improvements in mathematical reasoning and coding tasks.",
        "full_text": "OpenAI today announced GPT-5, its latest large language model. The company claims the model achieves 92% accuracy on graduate-level math problems, up from 74% with GPT-4. Independent benchmarks from Stanford show more modest improvements of about 5-8% across most tasks. The model uses a new architecture called 'deep reasoning chains' that allows it to break complex problems into substeps. Critics note that the benchmark improvements may not translate to real-world performance, and that the model's training data cutoff remains unclear.",
        "source_name": "TechCrunch",
    }

    result = analyzer.analyze_article(**test_article)
    print(json.dumps(result, ensure_ascii=False, indent=2))
