"""
source_ranker.py — 来源可靠性筛选与排序
- 白名单信誉分表（config 驱动）
- LLM 辅助评估未知来源
- 同事件多源报道选优
- 最终排序综合评分

用法:
    ranker = SourceRanker(config)
    items = ranker.score_and_filter(items)
    items = ranker.sort_by_relevance(items)
"""

import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# 默认来源信誉分（可被 config 覆盖）
# ---------------------------------------------------------------------------

DEFAULT_AUTHORITY = {
    # 官方博客 — 一手信息，最可信
    "OpenAI Blog": 5,
    "Anthropic News": 5,
    "Google AI Blog": 5,
    "DeepMind Blog": 5,
    "Meta Research": 4,
    "Hugging Face Blog": 4,

    # 学术 — 高可信但可能晦涩
    "ArXiv AI": 4,
    "ArXiv ML": 4,

    # 专业媒体 — 有编辑审核
    "MIT Tech Review": 4,
    "TechCrunch AI": 3,
    "The Verge AI": 3,
    "Ars Technica AI": 3,

    # 社区与视频 — 速度快但深度不一
    "Hacker News AI": 2,
    "AI Explained": 3,
    "Two Minute Papers": 3,
    "Yannic Kilcher": 3,
    "Matthew Berman": 2,

    # 中文源
    "36氪": 2,
    "少数派": 2,
}

# 来源类型的基准分（当来源不在白名单时使用）
SOURCE_TYPE_BASE = {
    "official": 4,    # 官方发布
    "paper": 4,       # 学术论文
    "research": 4,    # 研究机构
    "media": 3,       # 专业媒体
    "industry": 2,    # 行业资讯
    "community": 2,   # 社区讨论
    "video": 2,       # 视频内容
    "tech": 2,        # 科技资讯
}


class SourceRanker:
    """来源可靠性评估与排序"""

    def __init__(self, config: dict):
        # 从 config 加载来源信誉分，覆盖默认值
        self.authority: Dict[str, int] = {**DEFAULT_AUTHORITY}
        config_authority = config.get('source_authority', {})
        for k, v in config_authority.items():
            if k.startswith('_'):
                continue
            self.authority[k] = v

        # 从 config 提取来源类别映射
        self.source_category: Dict[str, str] = {}
        for lang in ('english', 'chinese'):
            for src in config.get('sources', {}).get(lang, []):
                name = src.get('name', '')
                cat = src.get('category', '')
                if name and cat:
                    self.source_category[name] = cat

        # LLM 评估缓存（本次运行内）
        self._llm_cache: Dict[str, int] = {}

    def get_authority(self, source_name: str) -> int:
        """获取来源信誉分"""
        # 先查白名单
        if source_name in self.authority:
            return self.authority[source_name]

        # 再查 LLM 缓存
        if source_name in self._llm_cache:
            return self._llm_cache[source_name]

        # 按来源类别给基准分
        cat = self.source_category.get(source_name, '')
        return SOURCE_TYPE_BASE.get(cat, 2)

    def evaluate_unknown_source(self, source_name: str, sample_title: str,
                                llm_analyzer=None) -> int:
        """用 LLM 评估未知来源的可信度（可选）

        如果没有 LLM analyzer，返回基于类别的默认分。
        评估结果会缓存到 self._llm_cache。
        """
        if source_name in self.authority or source_name in self._llm_cache:
            return self.get_authority(source_name)

        if llm_analyzer is None:
            return self.get_authority(source_name)

        # 构造评估 prompt
        prompt = f"""请评估以下新闻来源的可信度（1-5分）：
来源名称：{source_name}
示例标题：{sample_title}

评分标准：
5 = 官方一手信源（如 OpenAI 官方博客）
4 = 权威研究/专业媒体（如 Nature, MIT Tech Review）
3 = 知名科技媒体（如 TechCrunch, The Verge）
2 = 一般资讯/社区（如行业新闻站、个人博客）
1 = 不可靠（内容农场、标题党）

只返回一个数字（1-5），不要其他文字。"""

        try:
            response = llm_analyzer._call_api([
                {"role": "user", "content": prompt}
            ])
            score = int(re.search(r'[1-5]', response.strip()).group())
            self._llm_cache[source_name] = score
            print(f"  🔍 来源评估 [{source_name}]: {score}/5")
            return score
        except Exception:
            return self.get_authority(source_name)

    def score_and_filter(self, items: List[dict],
                         min_authority: int = 0,
                         llm_analyzer=None) -> List[dict]:
        """给每篇文章添加来源信誉分，可选过滤低分来源

        Args:
            items: 文章列表
            min_authority: 最低信誉分（低于此分的来源直接过滤），0=不过滤
            llm_analyzer: 可选的 LLM 分析器，用于评估未知来源

        Returns:
            添加了 _source_authority 字段的文章列表
        """
        result = []
        filtered = 0

        for item in items:
            source = item.get('source_name', '')

            # 未知来源且有 LLM → 评估
            if source not in self.authority and llm_analyzer:
                self.evaluate_unknown_source(
                    source, item.get('title', ''), llm_analyzer
                )

            authority = self.get_authority(source)
            item['_source_authority'] = authority

            if min_authority > 0 and authority < min_authority:
                filtered += 1
                continue

            result.append(item)

        if filtered > 0:
            print(f"  🛡️ 来源筛选移除 {filtered} 条低信誉来源")

        return result

    def enrich_cluster_info(self, items: List[dict]) -> List[dict]:
        """为同事件多源报道的胜出条目添加来源标注

        由 dedup_engine 的聚类选优后调用，在胜出条目上标注
        "其他 N 家也报道了此事"
        """
        for item in items:
            cluster_sources = item.get('_cluster_sources', [])
            cluster_size = item.get('_cluster_size', 0)
            if cluster_size > 1 and cluster_sources:
                unique_sources = list(set(s for s in cluster_sources if s))
                if unique_sources:
                    item['_also_reported_by'] = unique_sources
                    item['_report_count'] = cluster_size

        return items

    def sort_by_relevance(self, items: List[dict]) -> List[dict]:
        """综合排序：重要性 × 来源权威度 × 新鲜度 × 多源报道加成"""

        now = datetime.now(timezone.utc)

        def sort_score(item):
            analysis = item.get('analysis', {})
            importance = analysis.get('importance', 1)
            authority = item.get('_source_authority',
                                self.get_authority(item.get('source_name', '')))

            # 新鲜度衰减：24h 内=1.0, 48h=0.7, 更早=0.5
            freshness = 0.5
            if item.get('published'):
                try:
                    age_hours = (now - item['published'].replace(
                        tzinfo=timezone.utc if item['published'].tzinfo is None
                        else item['published'].tzinfo
                    )).total_seconds() / 3600
                    if age_hours < 24:
                        freshness = 1.0
                    elif age_hours < 48:
                        freshness = 0.7
                except Exception:
                    pass

            # 多源报道加成
            multi_source_bonus = 0
            report_count = item.get('_report_count', 0)
            if report_count >= 3:
                multi_source_bonus = 1.0
            elif report_count >= 2:
                multi_source_bonus = 0.5

            # 是否是新条目
            new_bonus = 0.5 if item.get('_is_new', True) else 0

            # 综合评分
            score = (
                importance * 0.40 +
                authority * 0.25 +
                freshness * 0.15 +
                multi_source_bonus * 0.10 +
                new_bonus * 0.10
            )

            return -score

        items.sort(key=sort_score)
        return items
