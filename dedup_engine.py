"""
dedup_engine.py — 两层去重引擎
第一层：精确去重（URL 哈希 + 标题哈希）
第二层：语义去重（TF-IDF 余弦相似度，纯标准库实现）
存储层：SQLite 持久化

用法:
    engine = DedupEngine("dedup.db")
    unique = engine.deduplicate(items)   # items: list[dict]
    engine.close()
"""

import hashlib
import math
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# 文本标准化
# ---------------------------------------------------------------------------

# 中文分词的简易实现：按字切分 + 英文按空格/标点切分
_SPLIT_RE = re.compile(r'[\w]+', re.UNICODE)
_CJK_RANGES = [
    (0x4E00, 0x9FFF),    # CJK Unified
    (0x3400, 0x4DBF),    # CJK Extension A
    (0xF900, 0xFAFF),    # CJK Compatibility
]


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str) -> List[str]:
    """中英文混合分词：中文按字，英文按词"""
    text = text.lower().strip()
    text = unicodedata.normalize('NFKC', text)
    tokens = []
    for word in _SPLIT_RE.findall(text):
        has_cjk = any(_is_cjk(ch) for ch in word)
        if has_cjk:
            # 中文逐字 + 双字 n-gram
            chars = [ch for ch in word if _is_cjk(ch)]
            tokens.extend(chars)
            for i in range(len(chars) - 1):
                tokens.append(chars[i] + chars[i + 1])
        else:
            if len(word) > 1:  # 过滤单字母
                tokens.append(word)
    return tokens


def text_hash(text: str) -> str:
    """文本指纹：标准化后 SHA256"""
    normalized = re.sub(r'\s+', '', text.lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]


def url_hash(url: str) -> str:
    """URL 指纹：去协议、去参数、去尾斜杠"""
    if not url:
        return ""
    url = url.strip().rstrip('/')
    url = re.sub(r'^https?://(www\.)?', '', url)
    url = re.sub(r'[?#].*$', '', url)
    return hashlib.sha256(url.lower().encode('utf-8')).hexdigest()[:32]


# ---------------------------------------------------------------------------
# TF-IDF 余弦相似度（纯标准库）
# ---------------------------------------------------------------------------

class TFIDFMatcher:
    """轻量级 TF-IDF 语义匹配器，无外部依赖"""

    def __init__(self):
        self._docs: List[List[str]] = []
        self._vectors: List[Dict[str, float]] = []
        self._df: Counter = Counter()
        self._n_docs: int = 0

    def add(self, text: str) -> int:
        """添加文档，返回索引"""
        tokens = tokenize(text)
        idx = self._n_docs
        self._docs.append(tokens)
        self._n_docs += 1

        # 更新 DF
        unique_tokens = set(tokens)
        self._df.update(unique_tokens)

        # 计算 TF 向量（先存原始 TF，similarity 时再算 IDF）
        tf = Counter(tokens)
        total = len(tokens) or 1
        self._vectors.append({t: c / total for t, c in tf.items()})

        return idx

    def similarity(self, idx_a: int, idx_b: int) -> float:
        """计算两个文档的 TF-IDF 余弦相似度"""
        va = self._vectors[idx_a]
        vb = self._vectors[idx_b]

        # 取交集词汇计算
        common = set(va.keys()) & set(vb.keys())
        if not common:
            return 0.0

        n = self._n_docs or 1
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0

        all_terms = set(va.keys()) | set(vb.keys())
        for t in all_terms:
            idf = math.log(n / (1 + self._df.get(t, 0)))
            a_val = va.get(t, 0) * idf
            b_val = vb.get(t, 0) * idf
            dot += a_val * b_val
            norm_a += a_val * a_val
            norm_b += b_val * b_val

        denom = math.sqrt(norm_a) * math.sqrt(norm_b)
        return dot / denom if denom > 0 else 0.0

    def _jaccard(self, idx_a: int, idx_b: int) -> float:
        """词集合 Jaccard 相似度（对短标题更鲁棒）"""
        ka = set(self._vectors[idx_a].keys())
        kb = set(self._vectors[idx_b].keys())
        if not ka or not kb:
            return 0.0
        return len(ka & kb) / len(ka | kb)

    def find_similar(self, idx: int, threshold: float = 0.6) -> List[Tuple[int, float]]:
        """找出与给定文档相似度超过阈值的所有文档

        使用 TF-IDF 余弦相似度为主，对短文本（token < 10）补充 Jaccard
        兜底检查，取两者较高值。解决短标题在 TF-IDF 空间中相似度偏低的问题。
        """
        results = []
        is_short = len(self._vectors[idx]) < 10
        for i in range(idx):  # 只跟之前的比
            sim = self.similarity(idx, i)
            # 短文本 Jaccard 兜底
            if sim < threshold and is_short:
                jac = self._jaccard(idx, i)
                if jac > sim:
                    sim = jac
            if sim >= threshold:
                results.append((i, sim))
        return results


# ---------------------------------------------------------------------------
# SQLite 存储层
# ---------------------------------------------------------------------------

class DedupDB:
    """SQLite 持久化去重数据库"""

    def __init__(self, db_path: str = "dedup.db"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url_hash TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                source TEXT DEFAULT '',
                first_seen TEXT NOT NULL,
                event_cluster TEXT DEFAULT '',
                UNIQUE(url_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_url_hash ON articles(url_hash);
            CREATE INDEX IF NOT EXISTS idx_title_hash ON articles(title_hash);
            CREATE INDEX IF NOT EXISTS idx_first_seen ON articles(first_seen);
            CREATE INDEX IF NOT EXISTS idx_cluster ON articles(event_cluster);
        """)
        self.conn.commit()

    def has_url(self, uhash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE url_hash=? LIMIT 1", (uhash,)
        ).fetchone()
        return row is not None

    def has_title(self, thash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM articles WHERE title_hash=? LIMIT 1", (thash,)
        ).fetchone()
        return row is not None

    def insert(self, uhash: str, thash: str, title: str, url: str,
               source: str = "", cluster: str = ""):
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO articles
                   (url_hash, title_hash, title, url, source, first_seen, event_cluster)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uhash, thash, title, url, source,
                 datetime.now(timezone.utc).isoformat(), cluster)
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass

    def get_recent_titles(self, hours: int = 72) -> List[Tuple[str, str, str]]:
        """返回最近 N 小时的 (title, source, event_cluster)"""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self.conn.execute(
            "SELECT title, source, event_cluster FROM articles WHERE first_seen >= ? ORDER BY first_seen DESC",
            (cutoff,)
        ).fetchall()
        return rows

    def cleanup(self, keep_days: int = 30):
        """清理过期记录"""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        self.conn.execute("DELETE FROM articles WHERE first_seen < ?", (cutoff,))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# 去重引擎主类
# ---------------------------------------------------------------------------

class DedupEngine:
    """两层去重引擎

    第一层：精确匹配（URL 哈希 + 标题哈希）
    第二层：语义匹配（TF-IDF 余弦相似度）
    """

    def __init__(self, db_path: str = "dedup.db",
                 semantic_threshold: float = 0.60,
                 recent_hours: int = 72):
        self.db = DedupDB(db_path)
        self.matcher = TFIDFMatcher()
        self.threshold = semantic_threshold
        self.recent_hours = recent_hours

        # 索引映射：matcher_idx → item info
        self._idx_map: Dict[int, dict] = {}

        # 加载最近的历史标题到 matcher，用于跟新条目做语义比较
        recent = self.db.get_recent_titles(hours=recent_hours)
        for title, source, cluster in recent:
            idx = self.matcher.add(title)
            self._idx_map[idx] = {
                'title': title, 'source': source,
                'cluster': cluster, '_from_db': True
            }

        self._db_count = len(recent)
        print(f"  📦 去重引擎已加载 {self._db_count} 条历史记录")

    def deduplicate(self, items: List[dict],
                    source_authority: Optional[Dict[str, int]] = None
                    ) -> List[dict]:
        """对文章列表进行两层去重

        Args:
            items: 文章列表，每条需有 title, link, source_name
            source_authority: 来源权威度字典，用于同事件多源时选最优

        Returns:
            去重后的文章列表
        """
        if source_authority is None:
            source_authority = {}

        hash_dupes = 0
        semantic_dupes = 0
        result = []

        # 事件聚类：cluster_id → [items]
        clusters: Dict[str, List[dict]] = {}
        cluster_counter = 0

        for item in items:
            title = item.get('title', '')
            link = item.get('link', '')
            source = item.get('source_name', '')

            uh = url_hash(link)
            th = text_hash(title)

            # ── 第一层：精确去重 ──
            if uh and self.db.has_url(uh):
                hash_dupes += 1
                continue

            if th and self.db.has_title(th):
                hash_dupes += 1
                continue

            # ── 第二层：语义去重 ──
            idx = self.matcher.add(title)
            similar = self.matcher.find_similar(idx, self.threshold)

            if similar:
                # 找到最相似的已有条目
                best_match_idx, best_sim = max(similar, key=lambda x: x[1])
                best_match = self._idx_map.get(best_match_idx, {})

                if best_match.get('_from_db'):
                    # 与历史库中的条目重复
                    semantic_dupes += 1
                    self.db.insert(uh, th, title, link, source,
                                   best_match.get('cluster', ''))
                    self._idx_map[idx] = {
                        'title': title, 'source': source,
                        'cluster': best_match.get('cluster', ''),
                        '_from_db': False, '_item': item
                    }
                    continue

                # 与本批次的已有条目重复 → 同事件聚类
                existing_cluster = best_match.get('cluster', '')
                if existing_cluster:
                    cluster_id = existing_cluster
                else:
                    cluster_counter += 1
                    cluster_id = f"evt_{cluster_counter}"
                    best_match['cluster'] = cluster_id
                    # 把之前那个也加入聚类，并从 result 移除（由聚类选优决定）
                    if '_item' in best_match:
                        prev_item = best_match['_item']
                        clusters.setdefault(cluster_id, []).append(prev_item)
                        if prev_item in result:
                            result.remove(prev_item)

                item['_cluster'] = cluster_id
                item['_sim_score'] = best_sim
                clusters.setdefault(cluster_id, []).append(item)

                self._idx_map[idx] = {
                    'title': title, 'source': source,
                    'cluster': cluster_id,
                    '_from_db': False, '_item': item
                }
                # 先不加入 result，等聚类选优
                continue

            # 全新条目
            self._idx_map[idx] = {
                'title': title, 'source': source,
                'cluster': '', '_from_db': False, '_item': item
            }
            self.db.insert(uh, th, title, link, source)
            result.append(item)

        # ── 聚类选优：同事件多源只保留最佳 ──
        for cluster_id, cluster_items in clusters.items():
            best = self._pick_best(cluster_items, source_authority)
            best['_cluster_size'] = len(cluster_items)
            best['_cluster_sources'] = [
                it.get('source_name', '') for it in cluster_items
                if it is not best
            ]
            result.append(best)
            semantic_dupes += len(cluster_items) - 1

            # 所有聚类条目都写入 DB
            for it in cluster_items:
                uh = url_hash(it.get('link', ''))
                th = text_hash(it.get('title', ''))
                self.db.insert(uh, th, it.get('title', ''),
                               it.get('link', ''),
                               it.get('source_name', ''), cluster_id)

        # 汇报
        total_removed = hash_dupes + semantic_dupes
        if total_removed > 0:
            parts = []
            if hash_dupes:
                parts.append(f"{hash_dupes} 条精确重复")
            if semantic_dupes:
                parts.append(f"{semantic_dupes} 条语义重复")
            print(f"  🔄 去重移除 {total_removed} 条（{'，'.join(parts)}）")

        # 清理过期数据
        self.db.cleanup(keep_days=30)

        return result

    @staticmethod
    def _pick_best(cluster_items: List[dict],
                   source_authority: Dict[str, int]) -> dict:
        """从同事件多源报道中选出最优的一条"""
        def score(item):
            authority = source_authority.get(item.get('source_name', ''), 2)
            # 有正文的优先
            has_body = 1 if item.get('full_text', '') else 0
            # 标题越长信息量可能越大
            title_len = min(len(item.get('title', '')), 100) / 100
            return authority * 3 + has_body * 2 + title_len

        return max(cluster_items, key=score)

    def close(self):
        self.db.close()
