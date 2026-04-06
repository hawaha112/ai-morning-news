#!/usr/bin/env python3
"""
AI Morning Briefing - 每日 AI 资讯聚合器 v2

流程：RSS 抓取 → 文章原文提取 → LLM Toulmin 分析 + 翻译 → 六区卡片 HTML 生成

用法:
    python3 fetch_news.py              # 抓取并生成页面
    python3 fetch_news.py --open       # 抓取、生成并在浏览器中打开
    python3 fetch_news.py --no-llm     # 跳过 LLM 分析（仅抓取）
"""

import json
import os
import sys
import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import escape
import concurrent.futures
import urllib.request
import urllib.error
import ssl
import time

# 本地模块
from llm_analyzer import LLMAnalyzer, create_analyzer_from_config
from dedup_engine import DedupEngine
from source_ranker import SourceRanker

# ═══════════════════════════════════════════════════════════════════════
# 第一部分：RSS 解析（纯标准库）
# ═══════════════════════════════════════════════════════════════════════

import xml.etree.ElementTree as ET


def _text(el, tag, namespaces=None):
    """安全提取子元素文本"""
    if namespaces:
        for ns_prefix, ns_uri in namespaces.items():
            child = el.find(f'{{{ns_uri}}}{tag}')
            if child is not None and child.text:
                return child.text.strip()
    child = el.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _parse_date(date_str):
    """尝试解析各种日期格式"""
    if not date_str:
        return None
    date_str = date_str.strip()
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    cleaned = re.sub(r'\s+[A-Z]{2,5}$', ' +0000', date_str)
    for fmt in formats[:2]:
        try:
            dt = datetime.strptime(cleaned, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _clean_html(text):
    """移除 HTML 标签，提取纯文本"""
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'&quot;', '"', text)
    text = re.sub(r'&#\d+;', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _extract_image(el, namespaces=None):
    """尝试从条目中提取图片 URL"""
    if namespaces and 'media' in namespaces:
        ns = namespaces['media']
        for tag in ['thumbnail', 'content']:
            media = el.find(f'{{{ns}}}{tag}')
            if media is not None:
                url = media.get('url', '')
                if url:
                    return url
    enc = el.find('enclosure')
    if enc is not None:
        enc_type = enc.get('type', '')
        if 'image' in enc_type:
            return enc.get('url', '')
    for tag in ['description', 'content', 'summary']:
        child = el.find(tag)
        if child is not None and child.text:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', child.text)
            if m:
                return m.group(1)
    if namespaces:
        for ns_prefix, ns_uri in namespaces.items():
            child = el.find(f'{{{ns_uri}}}encoded')
            if child is not None and child.text:
                m = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', child.text)
                if m:
                    return m.group(1)
    return ""


def parse_rss(xml_text):
    """解析 RSS/Atom feed，返回条目列表"""
    items = []
    try:
        namespaces = {}
        for m in re.finditer(r'xmlns:(\w+)=["\']([^"\']+)["\']', xml_text[:3000]):
            namespaces[m.group(1)] = m.group(2)
        xml_text = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#)', '&amp;', xml_text)
        xml_text = xml_text.lstrip('\ufeff')
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            cleaned = re.sub(r'<!\[CDATA\[.*?\]\]>', '', xml_text, flags=re.DOTALL)
            root = ET.fromstring(cleaned)
        ns = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''

        if 'atom' in ns.lower() or root.tag.endswith('feed'):
            atom_ns = ns if ns else 'http://www.w3.org/2005/Atom'
            for entry in root.findall(f'{{{atom_ns}}}entry') or root.findall('entry'):
                title_el = entry.find(f'{{{atom_ns}}}title') if atom_ns else entry.find('title')
                title = title_el.text.strip() if title_el is not None and title_el.text else ""
                link = ""
                for link_el in entry.findall(f'{{{atom_ns}}}link') or entry.findall('link'):
                    rel = link_el.get('rel', 'alternate')
                    if rel == 'alternate' or not link:
                        link = link_el.get('href', '')
                summary_el = entry.find(f'{{{atom_ns}}}summary') or entry.find('summary')
                content_el = entry.find(f'{{{atom_ns}}}content') or entry.find('content')
                summary = ""
                if content_el is not None and content_el.text:
                    summary = _clean_html(content_el.text)
                elif summary_el is not None and summary_el.text:
                    summary = _clean_html(summary_el.text)
                pub_el = (entry.find(f'{{{atom_ns}}}published')
                          or entry.find(f'{{{atom_ns}}}updated')
                          or entry.find('published') or entry.find('updated'))
                pub_date = _parse_date(pub_el.text if pub_el is not None else "")
                image = _extract_image(entry, namespaces)
                if title:
                    items.append({
                        'title': title, 'link': link,
                        'summary': summary[:800] if summary else "",
                        'published': pub_date, 'image': image,
                    })
        else:
            channel = root.find('channel') or root
            for item in channel.findall('item'):
                title = _text(item, 'title') or ""
                link = _text(item, 'link') or ""
                description = ""
                for tag in ['description', 'summary']:
                    desc = _text(item, tag)
                    if desc:
                        description = _clean_html(desc)
                        break
                content_encoded = ""
                if namespaces:
                    for ns_prefix, ns_uri in namespaces.items():
                        encoded = item.find(f'{{{ns_uri}}}encoded')
                        if encoded is not None and encoded.text:
                            content_encoded = _clean_html(encoded.text)
                            break
                best_summary = content_encoded if len(content_encoded) > len(description) else description
                pub_str = _text(item, 'pubDate') or _text(item, 'dc:date') or ""
                if not pub_str and namespaces:
                    for ns_prefix, ns_uri in namespaces.items():
                        pub_str = _text(item, 'date', {ns_prefix: ns_uri})
                        if pub_str:
                            break
                pub_date = _parse_date(pub_str)
                image = _extract_image(item, namespaces)
                if title:
                    items.append({
                        'title': title, 'link': link,
                        'summary': best_summary[:800] if best_summary else "",
                        'published': pub_date, 'image': image,
                    })
    except ET.ParseError as e:
        print(f"  [XML解析错误] {e}")
    except Exception as e:
        print(f"  [解析异常] {e}")
    return items


# ═══════════════════════════════════════════════════════════════════════
# 第二部分：文章正文抓取
# ═══════════════════════════════════════════════════════════════════════

def _make_nossl_opener():
    """创建跳过 SSL 验证的 opener（仅用于证书有问题的服务器）"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def _http_get(url, timeout=10):
    """通用 HTTP GET（优先验证 SSL，证书问题时自动降级）"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8',
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(800_000)
    except (ssl.SSLCertVerificationError, ssl.SSLError):
        opener = _make_nossl_opener()
        req = urllib.request.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            data = resp.read(800_000)
    for enc in ['utf-8', 'latin-1', 'gb2312', 'gbk']:
        try:
            return data.decode(enc)
        except:
            continue
    return data.decode('utf-8', errors='replace')


def _resolve_hn_real_url(hn_url):
    """Hacker News: 从评论页中提取真正的文章链接"""
    try:
        html = _http_get(hn_url, timeout=10)
        m = re.search(r'class="titleline"[^>]*>\s*<a\s+href="([^"]+)"', html)
        if m:
            real_url = m.group(1)
            if real_url.startswith('item?'):
                return ""
            return real_url
    except:
        pass
    return ""


def _fetch_youtube_description(yt_url):
    """YouTube: 从视频页提取描述文本"""
    try:
        html = _http_get(yt_url, timeout=12)
        m = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, re.IGNORECASE)
        desc = m.group(1) if m else ""
        m2 = re.search(r'"shortDescription"\s*:\s*"((?:[^"\\]|\\.){50,})"', html)
        if m2:
            long_desc = m2.group(1).encode().decode('unicode_escape', errors='replace')
            if len(long_desc) > len(desc):
                desc = long_desc
        if len(desc) < 50:
            m3 = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html, re.IGNORECASE)
            if m3 and len(m3.group(1)) > len(desc):
                desc = m3.group(1)
        return _clean_html(desc)
    except:
        return ""


def _scrape_youtube_channel(channel_id, max_items=10):
    """Fallback：当 YouTube RSS feed 返回 404 时，从频道页 HTML 提取视频列表

    解析 ytInitialData JSON，提取视频标题、链接、发布时间。
    比 RSS 更可靠，因为直接从前端页面数据提取。
    """
    # 尝试通过 channel_id 构造频道视频页 URL
    # 注意：不用 _http_get（800KB 限制会截断 ytInitialData），直接读完整页面
    channel_url = f"https://www.youtube.com/channel/{channel_id}/videos"
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        req = urllib.request.Request(channel_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                html = resp.read(2_000_000).decode('utf-8', errors='replace')
        except (ssl.SSLCertVerificationError, ssl.SSLError):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
            req = urllib.request.Request(channel_url, headers=headers)
            with opener.open(req, timeout=15) as resp:
                html = resp.read(2_000_000).decode('utf-8', errors='replace')
    except Exception as e:
        print(f"    ⚠️ YouTube 频道页抓取失败: {e}")
        return []

    # 提取 ytInitialData JSON
    m = re.search(r'ytInitialData\s*=\s*({.*?});</script>', html, re.DOTALL)
    if not m:
        return []

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    items = []
    now = datetime.now(timezone.utc)

    # 导航到视频列表
    tabs = data.get('contents', {}).get('twoColumnBrowseResultsRenderer', {}).get('tabs', [])
    for tab in tabs:
        tab_content = tab.get('tabRenderer', {}).get('content', {})
        section = tab_content.get('richGridRenderer', {})
        contents = section.get('contents', [])
        for entry in contents[:max_items]:
            vid = (entry.get('richItemRenderer', {})
                   .get('content', {})
                   .get('videoRenderer', {}))
            if not vid:
                continue

            title = vid.get('title', {}).get('runs', [{}])[0].get('text', '')
            vid_id = vid.get('videoId', '')
            if not title or not vid_id:
                continue

            link = f"https://www.youtube.com/watch?v={vid_id}"

            # 尝试从 publishedTimeText 推算发布时间
            pub_text = vid.get('publishedTimeText', {}).get('simpleText', '')
            pub_date = _estimate_youtube_date(pub_text, now)

            # 简短描述
            desc_runs = vid.get('descriptionSnippet', {}).get('runs', [])
            desc = ' '.join(r.get('text', '') for r in desc_runs) if desc_runs else ''

            # 缩略图
            thumbs = vid.get('thumbnail', {}).get('thumbnails', [])
            image = thumbs[-1].get('url', '') if thumbs else ''

            items.append({
                'title': title,
                'link': link,
                'summary': desc[:800] if desc else '',
                'published': pub_date,
                'image': image,
            })
        if items:
            break

    return items


def _estimate_youtube_date(pub_text, now):
    """从 YouTube 的相对时间文本推算大致日期

    支持两种格式：
    - 全称: '3 days ago', '2 weeks ago', '1 year ago'
    - 缩写: '3d ago', '2w ago', '4h ago'
    """
    if not pub_text:
        return None
    pub_text = pub_text.lower().strip()
    try:
        # 全称格式
        m = re.search(r'(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago', pub_text)
        if m:
            n, unit = int(m.group(1)), m.group(2)
        else:
            # 缩写格式: "4w ago", "3d ago", etc.
            m = re.search(r'(\d+)\s*([smhdwy])\w*\s+ago', pub_text)
            if m:
                n = int(m.group(1))
                abbrev = m.group(2)
                unit = {'s': 'second', 'm': 'minute', 'h': 'hour',
                        'd': 'day', 'w': 'week', 'y': 'year'}.get(abbrev, '')
            else:
                return None

        deltas = {
            'second': timedelta(seconds=n),
            'minute': timedelta(minutes=n),
            'hour': timedelta(hours=n),
            'day': timedelta(days=n),
            'week': timedelta(weeks=n),
            'month': timedelta(days=n * 30),
            'year': timedelta(days=n * 365),
        }
        if unit in deltas:
            return now - deltas[unit]
    except Exception:
        pass
    return None


def _fetch_github_readme(gh_url):
    """GitHub: 从 API 获取 README 内容"""
    try:
        m = re.match(r'https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$', gh_url)
        if not m:
            return ""
        owner, repo = m.group(1), m.group(2)
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        headers = {'Accept': 'application/vnd.github.v3.raw', 'User-Agent': 'Mozilla/5.0'}
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            readme = resp.read(100_000).decode('utf-8', errors='replace')
        readme = re.sub(r'!\[[^\]]*\]\([^)]+\)', '', readme)
        readme = re.sub(r'\[[^\]]*\]\([^)]+\)', '', readme)
        readme = re.sub(r'#{1,6}\s*', '', readme)
        readme = re.sub(r'[*_`~]{1,3}', '', readme)
        readme = re.sub(r'\n{3,}', '\n\n', readme)
        return readme[:2000].strip()
    except:
        return ""


def _extract_article_body(html_text):
    """从 HTML 中提取文章正文，多策略级联"""
    if not html_text:
        return ""
    for tag in ['script', 'style', 'nav', 'header', 'footer', 'aside',
                'noscript', 'iframe', 'form', 'svg', 'button']:
        html_text = re.sub(rf'<{tag}[\s>].*?</{tag}>', ' ', html_text,
                           flags=re.DOTALL | re.IGNORECASE)
    candidates = []

    # 策略1: <article> 标签
    for m in re.finditer(r'<article[^>]*>(.*?)</article>', html_text,
                         re.DOTALL | re.IGNORECASE):
        text = _clean_html(m.group(1))
        if len(text) > 100:
            candidates.append(('article_tag', text))

    # 策略2: 语义化 class/id
    semantic_patterns = [
        r'(?:article[_-]?body|post[_-]?content|entry[_-]?content|'
        r'article[_-]?content|story[_-]?body|blog[_-]?post|'
        r'main[_-]?content|article__body|post__body|'
        r'c-entry-content|post-full-content|article-text|'
        r'single[_-]?content|page[_-]?content)',
    ]
    for pat in semantic_patterns:
        for m in re.finditer(
            rf'<(?:div|section|main)[^>]*(?:class|id)="[^"]*{pat}[^"]*"[^>]*>(.*?)</(?:div|section|main)>',
            html_text, re.DOTALL | re.IGNORECASE):
            text = _clean_html(m.group(1))
            if len(text) > 100:
                candidates.append(('semantic_class', text))

    # 策略3: JSON-LD
    for m in re.finditer(
        r'<script\s+type="application/ld\+json"[^>]*>(.*?)</script>',
        html_text, re.DOTALL | re.IGNORECASE):
        try:
            ld = json.loads(m.group(1))
            if isinstance(ld, list):
                ld = ld[0]
            body = ld.get('articleBody', '') or ld.get('description', '')
            if body and len(body) > 80:
                candidates.append(('json_ld', _clean_html(body)))
        except:
            pass

    # 策略4: meta description
    meta_desc = ""
    for m in re.finditer(
        r'<meta\s+(?:name="description"|property="og:description")\s+content="([^"]*)"',
        html_text, re.IGNORECASE):
        if len(m.group(1)) > len(meta_desc):
            meta_desc = m.group(1)
    if meta_desc and len(meta_desc) > 60:
        candidates.append(('meta_desc', _clean_html(meta_desc)))

    # 策略5: <p> 段落
    paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', html_text,
                            re.DOTALL | re.IGNORECASE)
    good_ps = []
    for p in paragraphs:
        clean = _clean_html(p)
        if len(clean) > 40:
            tag_ratio = len(re.findall(r'<[^>]+>', p)) / max(len(clean), 1)
            if tag_ratio < 0.1:
                good_ps.append(clean)
    if good_ps:
        combined = '。'.join(good_ps[:20])
        if len(combined) > 100:
            candidates.append(('paragraphs', combined))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: len(x[1]), reverse=True)
    best = candidates[0][1]

    # 去噪
    noise_patterns = [
        r'Subscribe to.*?(?:newsletter|updates)[.\s]',
        r'Sign up for.*?(?:newsletter|free)[.\s]',
        r'Share this.*?(?:article|story)[.\s]',
        r'Related (?:articles?|stories|posts)[.\s]',
        r'(?:Cookie|Privacy) (?:policy|notice)[.\s]',
        r'Advertisement[.\s]',
        r'Follow us on[.\s]',
    ]
    for np in noise_patterns:
        best = re.sub(np, ' ', best, flags=re.IGNORECASE)
    best = re.sub(r'\s+', ' ', best).strip()
    return best[:4000]


def _fetch_article_text(url, source_name=""):
    """根据来源智能选择抓取策略"""
    if not url or url == '#':
        return ""
    try:
        if 'news.ycombinator.com' in url:
            real_url = _resolve_hn_real_url(url)
            if not real_url:
                return ""
            return _fetch_article_text(real_url, source_name)
        if 'youtube.com' in url or 'youtu.be' in url:
            return _fetch_youtube_description(url)
        if re.match(r'https?://(?:gist\.)?github\.com/', url):
            return _fetch_github_readme(url)
        if url.lower().endswith('.pdf'):
            return ""
        html = _http_get(url, timeout=10)
        return _extract_article_body(html)
    except Exception:
        return ""


def enrich_articles_with_content(items):
    """批量抓取文章原文"""
    print(f"\n📖 抓取文章原文...")
    to_fetch = [i for i, item in enumerate(items) if len(item.get('summary', '')) < 100]
    print(f"  🔍 共 {len(to_fetch)} 篇需要补充原文...")

    def _fetch_one(idx):
        item = items[idx]
        return idx, _fetch_article_text(item.get('link', ''), item.get('source_name', ''))

    success = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch_one, idx): idx for idx in to_fetch}
        for future in concurrent.futures.as_completed(futures):
            try:
                idx, text = future.result()
                if text and len(text) > 30:
                    items[idx]['article_text'] = text
                    success += 1
            except:
                pass
    print(f"  📥 成功抓取 {success}/{len(to_fetch)} 篇原文")

    # Fallback：对抓取失败且无 article_text 的条目，用 RSS summary 兜底
    fallback_count = 0
    for item in items:
        if not item.get('article_text') and item.get('summary'):
            summary = item['summary'].strip()
            if len(summary) > 30:
                item['article_text'] = summary
                fallback_count += 1
    if fallback_count > 0:
        print(f"  📋 {fallback_count} 篇使用 RSS 摘要兜底")

    return items


# ═══════════════════════════════════════════════════════════════════════
# 第三部分：RSS 源抓取 & 源健康度监控
# ═══════════════════════════════════════════════════════════════════════

class SourceHealthTracker:
    """跟踪每个源的抓取健康状态，连续失败超过阈值时报警"""

    def __init__(self, health_path: str, alert_threshold: int = 3):
        self.health_path = Path(health_path)
        self.alert_threshold = alert_threshold
        self.data = self._load()

    def _load(self) -> dict:
        if self.health_path.exists():
            try:
                with open(self.health_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save(self):
        try:
            with open(self.health_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  ⚠️ 健康度数据保存失败: {e}")

    def record_success(self, source_name: str, item_count: int):
        self.data[source_name] = {
            'status': 'ok',
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': item_count,
            'consecutive_failures': 0,
            'last_error': '',
        }

    def record_failure(self, source_name: str, error: str):
        prev = self.data.get(source_name, {})
        failures = prev.get('consecutive_failures', 0) + 1
        self.data[source_name] = {
            'status': 'failing',
            'last_success': prev.get('last_success', ''),
            'last_count': prev.get('last_count', 0),
            'consecutive_failures': failures,
            'last_error': str(error)[:200],
        }

    def get_alerts(self) -> list:
        """返回连续失败超过阈值的源列表"""
        alerts = []
        for name, info in self.data.items():
            fails = info.get('consecutive_failures', 0)
            if fails >= self.alert_threshold:
                last_ok = info.get('last_success', '从未成功')
                err = info.get('last_error', '未知错误')
                alerts.append({
                    'source': name,
                    'consecutive_failures': fails,
                    'last_success': last_ok,
                    'last_error': err,
                })
        return alerts

    def print_report(self):
        alerts = self.get_alerts()
        if not alerts:
            return
        print(f"\n🚨 源健康度警报：{len(alerts)} 个源连续失败")
        for a in alerts:
            print(f"  ⛔ {a['source']}: 连续 {a['consecutive_failures']} 次失败"
                  f" | 上次成功: {a['last_success'][:10] if a['last_success'] != '从未成功' else '从未成功'}"
                  f" | 错误: {a['last_error'][:60]}")


def fetch_feed(source, max_items=10, max_age_hours=48, health_tracker=None):
    """抓取单个 RSS 源"""
    name = source['name']
    url = source['url']
    print(f"  📡 抓取 {name}...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/rss+xml, application/xml, text/xml, */*',
    }

    class SmartRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return urllib.request.Request(newurl, headers=dict(req.header_items()))

    try:
        req = urllib.request.Request(url, headers=headers)
        try:
            opener = urllib.request.build_opener(SmartRedirectHandler)
            resp_ctx = opener.open(req, timeout=20)
        except (ssl.SSLCertVerificationError, ssl.SSLError):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                SmartRedirectHandler, urllib.request.HTTPSHandler(context=ctx))
            req = urllib.request.Request(url, headers=headers)
            resp_ctx = opener.open(req, timeout=20)
        with resp_ctx as resp:
            data = resp.read()
            for encoding in ['utf-8', 'latin-1', 'gb2312', 'gbk']:
                try:
                    xml_text = data.decode(encoding)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                xml_text = data.decode('utf-8', errors='replace')

        items = parse_rss(xml_text)

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=max_age_hours)
        filtered = [i for i in items if not i['published'] or i['published'] >= cutoff]
        if not filtered and items:
            filtered = items[:min(5, max_items)]

        for item in filtered[:max_items]:
            item['source_name'] = source['name']
            item['source_icon'] = source['icon']
            item['source_color'] = source['color']
            item['source_category'] = source['category']

        count = len(filtered[:max_items])
        print(f"  ✅ {name}: 获取 {count} 条")
        if health_tracker:
            health_tracker.record_success(name, count)
        return filtered[:max_items]

    except Exception as e:
        # YouTube RSS 404 fallback：抓频道页
        if 'youtube.com/feeds/' in url:
            m = re.search(r'channel_id=([A-Za-z0-9_-]+)', url)
            if m:
                channel_id = m.group(1)
                print(f"  🔄 {name}: RSS 失败, 尝试频道页抓取...")
                items = _scrape_youtube_channel(channel_id, max_items)
                if items:
                    now = datetime.now(timezone.utc)
                    cutoff = now - timedelta(hours=max_age_hours)
                    filtered = [i for i in items
                                if not i['published'] or i['published'] >= cutoff]
                    if not filtered and items:
                        filtered = items[:min(5, max_items)]
                    for item in filtered[:max_items]:
                        item['source_name'] = source['name']
                        item['source_icon'] = source['icon']
                        item['source_color'] = source['color']
                        item['source_category'] = source['category']
                    count = len(filtered[:max_items])
                    print(f"  ✅ {name}: 频道页抓取 {count} 条")
                    if health_tracker:
                        health_tracker.record_success(name, count)
                    return filtered[:max_items]

        print(f"  ❌ {name}: {e}")
        if health_tracker:
            health_tracker.record_failure(name, e)
        return []


# ═══════════════════════════════════════════════════════════════════════
# 第三-B部分：去重 & 历史追踪
# ═══════════════════════════════════════════════════════════════════════

def _normalize_url(url):
    """规范化 URL 用于去重比较"""
    if not url:
        return ""
    url = url.strip().rstrip('/')
    url = re.sub(r'^https?://(www\.)?', '', url)
    url = re.sub(r'[?#].*$', '', url)
    return url.lower()


def _title_similarity(t1, t2):
    """简易标题相似度（基于词集合 Jaccard）"""
    if not t1 or not t2:
        return 0.0
    words1 = set(re.findall(r'\w+', t1.lower()))
    words2 = set(re.findall(r'\w+', t2.lower()))
    if not words1 or not words2:
        return 0.0
    return len(words1 & words2) / len(words1 | words2)


# AI 相关关键词（用于对非 ai_only 源做预过滤）
_AI_KEYWORDS = re.compile(
    r'(?i)\b(?:'
    r'ai|artificial.intelligence|machine.learning|deep.learning|'
    r'neural.net|llm|large.language.model|gpt|chatgpt|openai|'
    r'anthropic|claude|gemini|copilot|midjourney|stable.diffusion|'
    r'transformer|diffusion.model|reinforcement.learning|'
    r'computer.vision|natural.language|nlp|nlu|'
    r'generative|gen.?ai|agi|alignment|'
    r'robot|autonomous|self.driving|autopilot|'
    r'chip|gpu|tpu|nvidia|cuda|'
    r'hugging.?face|pytorch|tensorflow|'
    r'token|embedding|fine.?tun|rag|vector.?db|'
    r'agent|multi.?modal|reasoning|'
    r'deepseek|mistral|llama|qwen|'
    # 中文
    r'人工智能|机器学习|深度学习|大模型|大语言模型|'
    r'神经网络|自然语言|智能体|算力|芯片|'
    r'自动驾驶|具身智能|生成式|训练|推理|'
    r'向量|微调|对齐|多模态'
    r')\b'
)


def _keyword_prefilter(items, ai_only_sources):
    """对非 ai_only 源的条目做关键词预筛选

    ai_only 源的条目全部保留（已经是 AI 频道）。
    非 ai_only 源的条目需要标题或摘要中含 AI 关键词才保留。
    """
    result = []
    filtered = 0
    for item in items:
        source = item.get('source_name', '')
        if source in ai_only_sources:
            result.append(item)
            continue
        # 非 AI 专用源 → 用关键词判断
        text = (item.get('title', '') + ' ' + item.get('summary', '')[:300]).lower()
        if _AI_KEYWORDS.search(text):
            result.append(item)
        else:
            filtered += 1
    if filtered > 0:
        print(f"  🔍 关键词预过滤移除 {filtered} 条明显非 AI 内容（节省 LLM 调用）")
    return result


def deduplicate_items(items):
    """去重：基于 URL 和标题相似度"""
    seen_urls = {}
    result = []
    for item in items:
        url_key = _normalize_url(item.get('link', ''))
        title = item.get('title', '')

        # URL 完全相同
        if url_key and url_key in seen_urls:
            continue

        # 标题相似度 > 0.7
        is_dup = False
        for existing in result:
            if _title_similarity(title, existing.get('title', '')) > 0.7:
                is_dup = True
                break
        if is_dup:
            continue

        if url_key:
            seen_urls[url_key] = True
        result.append(item)

    removed = len(items) - len(result)
    if removed > 0:
        print(f"  🔄 去重移除 {removed} 条重复内容")
    return result


def load_history(history_path, max_days=7):
    """加载历史记录"""
    if not history_path.exists():
        return {}
    try:
        with open(history_path, 'r', encoding='utf-8') as f:
            history = json.load(f)
        # 清理过期记录
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()
        history = {k: v for k, v in history.items() if v.get('date', '') >= cutoff}
        return history
    except Exception as e:
        print(f"  ⚠️ 历史记录加载失败: {e}")
        return {}


def save_history(history_path, history, new_items):
    """保存历史记录"""
    now = datetime.now(timezone.utc).isoformat()
    for item in new_items:
        url_key = _normalize_url(item.get('link', ''))
        if url_key:
            history[url_key] = {
                'title': item.get('title', ''),
                'date': now,
            }
    try:
        with open(history_path, 'w', encoding='utf-8') as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ⚠️ 历史记录保存失败: {e}")


def mark_seen_items(items, history):
    """标记已在历史中出现过的条目"""
    for item in items:
        url_key = _normalize_url(item.get('link', ''))
        item['_is_new'] = url_key not in history if url_key else True
    new_count = sum(1 for i in items if i.get('_is_new', True))
    old_count = len(items) - new_count
    if old_count > 0:
        print(f"  📜 {new_count} 条新内容，{old_count} 条往期已收录")
    return items


# ═══════════════════════════════════════════════════════════════════════
# 第四部分：HTML 生成
# ═══════════════════════════════════════════════════════════════════════

# -- 来源类型中文映射 --
SOURCE_TYPE_LABELS = {
    "paper": "学术论文",
    "news": "新闻报道",
    "official": "官方发布",
    "opinion": "观点文章",
    "community": "社区讨论",
    "video": "视频",
}




def _importance_dots(level):
    """生成重要性圆点 HTML — Tufte 风格，最小有效差异"""
    filled = min(max(level, 1), 5)
    colors = {1: '#555', 2: '#888', 3: '#c9a227', 4: '#e8913a', 5: '#e05252'}
    active_color = colors.get(filled, '#888')
    return ''.join(
        f'<span class="imp-dot" style="color:{active_color if i < filled else "rgba(255,255,255,0.12)"}">'
        f'{"●" if i < filled else "○"}</span>'
        for i in range(5)
    )


def _importance_label(level):
    """重要性文字标签"""
    labels = {1: "一般", 2: "关注", 3: "重要", 4: "很重要", 5: "重大事件"}
    return labels.get(level, "")


def generate_html(all_items, config, digest=None):
    """生成六区卡片模型的 HTML 页面 — 基于 Tufte 信噪比原则重新设计"""
    now = datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    weekday_map = {0: '一', 1: '二', 2: '三', 3: '四', 4: '五', 5: '六', 6: '日'}
    weekday = weekday_map[now.weekday()]
    time_str = now.strftime("%H:%M")

    # 统计
    by_source = {}
    for item in all_items:
        src = item['source_name']
        by_source.setdefault(src, []).append(item)

    total = len(all_items)
    sources_count = len(by_source)

    avg_importance = 0
    imp_items = [i for i in all_items if i.get('analysis', {}).get('importance', 0) > 0]
    if imp_items:
        avg_importance = sum(i['analysis']['importance'] for i in imp_items) / len(imp_items)

    # 收集分类
    all_categories = set()
    for item in all_items:
        for cat in item.get('analysis', {}).get('categories', []):
            all_categories.add(cat)

    # ── 构建卡片 ──
    cards_html = ""
    modal_data = []

    for idx, item in enumerate(all_items):
        analysis = item.get('analysis', {})
        summary = escape(analysis.get('summary', item.get('title', '')[:100]))
        why_it_matters = escape(analysis.get('why_it_matters', ''))
        key_details = analysis.get('key_details', [])
        importance = analysis.get('importance', 1)
        categories = analysis.get('categories', ['其他'])
        source_type = analysis.get('source_type', 'news')
        reading_minutes = analysis.get('reading_minutes', 1)

        pub_str = ""
        if item.get('published'):
            try:
                pub_str = item['published'].strftime("%m-%d %H:%M")
            except:
                pass

        link = escape(item.get('link', '#'))
        icon = item.get('source_icon', '📰')
        source_name = escape(item.get('source_name', ''))
        cat_data = '|'.join(categories)
        image_url = escape(item.get('image', ''))

        # 是否是高重要性卡片（4-5）
        is_featured = importance >= 4

        # Z1: bare minimum — category + reading time
        cat_text = ' · '.join(escape(c) for c in categories[:2])
        z1_html = f'''<div class="z1">
            <span class="z1-left">{cat_text}</span>
            <span class="z1-meta">{reading_minutes} min</span>
        </div>'''

        # 图片区域
        img_html = ""
        if image_url:
            img_html = f'<div class="card-img" style="background-image:url(\'{image_url}\')"></div>'

        # Z2: 一句话 — the card IS this sentence
        z2_html = f'<div class="z2">{summary}</div>'

        # Z3: natural reading flow — why it matters, then key points
        z3_inner = ""
        if why_it_matters:
            z3_inner += f'<div class="z3-why">{why_it_matters}</div>'
        if key_details:
            details_items = ""
            for d in key_details[:5]:
                d_text = escape(d) if isinstance(d, str) else escape(str(d))
                details_items += f'<div class="z3-detail">{d_text}</div>'
            z3_inner += f'<div class="z3-details">{details_items}</div>'
        z3_html = f'<div class="z3">{z3_inner}</div>' if z3_inner else ""

        # Z5: source — just the fact, no call-to-action
        z5_html = f'<div class="z5">{icon} {source_name} · {pub_str}</div>'

        # 组装卡片
        featured_cls = " card--featured" if is_featured else ""
        cards_html += f'''
        <div class="card{featured_cls}" data-cat="{escape(cat_data)}" data-idx="{idx}"
             style="animation-delay:{min(idx * 25, 500)}ms"
             onclick="openModal({idx})">
            {img_html}
            <div class="card-body">
                {z1_html}
                {z2_html}
                {z3_html}
                {z5_html}
            </div>
        </div>'''

        # 弹窗数据 — 包含卡片上没有的深度字段
        modal_data.append({
            "title": item.get('title', ''),
            "summary": analysis.get('summary', item.get('title', '')[:100]),
            "background": analysis.get('background', ''),
            "deep_analysis": analysis.get('deep_analysis', ''),
            "importance": importance,
            "categories": categories,
            "source_type": source_type,
            "source_name": source_name,
            "source_icon": icon,
            "link": item.get('link', '#'),
            "pub_date": pub_str,
            "image": item.get('image', ''),
            "reading_minutes": reading_minutes,
        })

    modal_json = json.dumps(modal_data, ensure_ascii=False)

    # 筛选按钮 — 只显示实际存在的分类
    filter_html = '<button class="f-btn active" data-filter="all">全部</button>\n'
    ordered_cats = ['大模型发布', '开源生态', 'AI 政策监管', '芯片与算力', '产品与应用',
                    '安全与对齐', '融资与商业', '学术研究', 'AI 工具', '具身智能',
                    '自动驾驶', 'AI 医疗', 'AI 编程', '行业观点', '其他']
    for cat in ordered_cats:
        if cat in all_categories:
            filter_html += f'<button class="f-btn" data-filter="{escape(cat)}">{escape(cat)}</button>\n'

    # 今日速览 — 仅编辑导语，不列 top stories
    briefing_html = ""
    if digest and digest.get('editorial'):
        editorial = escape(digest.get('editorial', ''))
        briefing_html = f'''
    <section class="briefing">
        <h2 class="br-title">今日速览</h2>
        <p class="br-editorial">{editorial}</p>
    </section>'''

    # ══════════════════════════════════════════════════════════
    # 完整 HTML
    # ══════════════════════════════════════════════════════════
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 早报 · {date_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
/* ═════ Design Tokens ═════ */
:root {{
    --bg: #0b0b12;
    --bg-card: rgba(255,255,255,0.03);
    --bg-card-hover: rgba(255,255,255,0.055);
    --text-100: #e4e4ec;
    --text-70: rgba(228,228,236,0.70);
    --text-50: rgba(228,228,236,0.50);
    --text-35: rgba(228,228,236,0.35);
    --border: rgba(255,255,255,0.06);
    --border-hover: rgba(255,255,255,0.10);
    --accent: #6b8afd;
    --green: #5a9e6f;
    --amber: #c9a227;
    --red: #c05050;
    --radius: 12px;
    --sans: 'Inter','Noto Sans SC',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
    --serif: 'Noto Serif SC',Georgia,serif;
}}
*{{ margin:0; padding:0; box-sizing:border-box; }}
body {{
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text-100);
    line-height: 1.6;
    -webkit-font-smoothing: antialiased;
}}

/* ═════ Header — 极简 ═════ */
.header {{
    position: sticky; top: 0; z-index: 100;
    background: rgba(11,11,18,0.92);
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 10px 20px;
}}
.header-inner {{
    max-width: 1120px; margin: 0 auto;
    display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
}}
.h-title {{
    font-family: var(--serif); font-size: 17px; font-weight: 700;
    color: var(--text-100); white-space: nowrap; letter-spacing: -0.3px;
}}
.h-date {{ font-size: 11px; color: var(--text-35); white-space: nowrap; }}
.h-stats {{
    display: flex; gap: 10px; margin-left: auto;
    font-size: 10px; color: var(--text-35); letter-spacing: 0.3px;
}}
.h-stats b {{ color: var(--accent); font-weight: 600; }}
.search-wrap {{ position: relative; flex: 1; max-width: 260px; }}
.search-wrap::before {{
    content: '⌕'; position: absolute; left: 9px; top: 50%;
    transform: translateY(-50%); font-size: 12px; color: var(--text-35);
    pointer-events: none;
}}
.search-box {{
    width: 100%; padding: 7px 10px 7px 28px;
    background: rgba(255,255,255,0.035); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text-100); font-size: 12px; outline: none;
    transition: border-color 0.2s;
}}
.search-box:focus {{ border-color: var(--accent); }}

/* ═════ Filter ═════ */
.filter-bar {{
    max-width: 1120px; margin: 14px auto 0; padding: 0 20px;
    display: flex; gap: 5px; flex-wrap: wrap;
}}
.f-btn {{
    padding: 4px 12px; border: 1px solid var(--border);
    background: transparent; color: var(--text-35);
    border-radius: 14px; font-size: 11px; font-weight: 500;
    cursor: pointer; transition: all 0.2s; white-space: nowrap;
}}
.f-btn:hover {{ border-color: rgba(107,138,253,0.4); color: var(--text-50); }}
.f-btn.active {{ background: var(--accent); border-color: var(--accent); color: #fff; }}

/* ═════ Briefing ═════ */
.briefing {{
    max-width: 1120px; margin: 20px auto 0; padding: 0 20px;
}}
.briefing > * {{
    max-width: 680px;
}}
.br-title {{
    font-family: var(--serif); font-size: 14px; font-weight: 700;
    color: var(--text-50); letter-spacing: 1px; text-transform: uppercase;
    margin-bottom: 10px;
}}
.br-editorial {{
    font-size: 14px; line-height: 1.75; color: var(--text-70);
    margin-bottom: 14px;
    border-left: 2px solid var(--accent); padding-left: 14px;
}}

/* ═════ Grid — masonry columns ═════ */
.grid {{
    max-width: 1120px; margin: 18px auto; padding: 0 14px;
    columns: 3; column-gap: 10px;
}}
@media (max-width:1024px) {{ .grid {{ columns: 2; }} }}
@media (max-width:640px)  {{ .grid {{ columns: 1; padding: 0 10px; }} }}

/* ═════ Card — 最小有效差异 ═════ */
.card {{
    break-inside: avoid;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    margin-bottom: 10px;
    cursor: pointer;
    overflow: hidden;
    transition: border-color 0.2s, box-shadow 0.25s, transform 0.25s;
    opacity: 0; transform: translateY(8px);
    animation: cardIn 0.35s ease forwards;
    background: var(--bg-card);
}}
.card:hover {{
    border-color: rgba(255,255,255,0.10);
    background: var(--bg-card-hover);
}}
.card.hidden {{ display: none !important; }}
@keyframes cardIn {{ to {{ opacity:1; transform:translateY(0); }} }}

/* card image */
.card-img {{
    width: 100%; height: 140px;
    background-size: cover; background-position: center;
}}
@media (max-width:640px) {{ .card-img {{ height: 120px; }} }}

.card-body {{ padding: 14px 16px; }}

/* featured — subtle warm background, not a colored border */
.card--featured {{
    background: rgba(107,138,253,0.03);
    border-color: rgba(107,138,253,0.12);
}}

/* Z1 — bare minimum: category + time */
.z1 {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px;
}}
.z1-left {{
    font-size: 11px; color: var(--text-35); letter-spacing: 0.2px;
}}
.z1-meta {{
    font-size: 11px; color: var(--text-35); white-space: nowrap;
}}

/* Z2 — the card IS this sentence */
.z2 {{
    font-family: var(--serif);
    font-size: 15px; font-weight: 700; line-height: 1.55;
    color: var(--text-100); margin-bottom: 10px;
}}

/* Z3 — natural reading flow, no decorative borders */
.z3 {{ margin-bottom: 10px; }}
.z3-why {{
    font-size: 13px; line-height: 1.6; color: var(--text-60, rgba(228,228,236,0.60));
    margin-bottom: 8px;
}}
.z3-details {{
    display: flex; flex-direction: column; gap: 4px;
}}
.z3-detail {{
    font-size: 12px; color: var(--text-50); line-height: 1.5;
    padding-left: 12px; position: relative;
}}
.z3-detail::before {{
    content: '·'; position: absolute; left: 2px;
    color: var(--text-35); font-weight: 700;
}}

/* Z5 — source only, no button, no announcement */
.z5 {{
    font-size: 10px; color: var(--text-35);
    padding-top: 8px; border-top: 1px solid var(--border);
}}

/* ═════ Modal — clean ═════ */
.modal-overlay {{
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.70);
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px);
    z-index: 1000; display: none;
    align-items: flex-start; justify-content: center;
    padding: 5vh 14px; overflow-y: auto;
}}
.modal-overlay.show {{ display: flex; }}
.modal {{
    background: #13131f;
    border: 1px solid var(--border);
    border-radius: 14px;
    max-width: 600px; width: 100%;
    padding: 22px; margin: auto;
    animation: mUp 0.2s ease;
    box-shadow: 0 12px 40px rgba(0,0,0,0.5);
}}
@keyframes mUp {{ from {{ opacity:0; transform:translateY(10px); }} to {{ opacity:1; transform:translateY(0); }} }}
.m-close {{
    float: right; background: none; border: none;
    color: var(--text-35); width: 28px; height: 28px; border-radius: 8px;
    cursor: pointer; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.15s;
}}
.m-close:hover {{ background: rgba(255,255,255,0.06); color: var(--text-100); }}
.m-pills {{
    display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
    margin-bottom: 10px; font-size: 11px; color: var(--text-35);
}}
.m-pill-sep {{ opacity: 0.4; }}
.m-title {{
    font-family: var(--serif); font-size: 18px; font-weight: 700;
    line-height: 1.4; color: var(--text-100); margin-bottom: 8px;
}}
.m-summary {{
    font-size: 13px; line-height: 1.65; color: var(--text-60, rgba(228,228,236,0.60));
    margin-bottom: 16px;
}}
.m-img {{
    width: 100%; max-height: 240px; object-fit: cover;
    border-radius: 8px; margin-bottom: 14px;
}}
/* deep-dive — natural reading flow, no decorative borders */
.m-deep {{
    display: flex; flex-direction: column; gap: 18px;
    padding: 16px 0 0; border-top: 1px solid var(--border);
}}
.m-deep-section {{ }}
.m-deep-label {{
    font-size: 11px; font-weight: 600; color: var(--text-50);
    letter-spacing: 0.1px; margin-bottom: 6px;
}}
.m-deep-text {{
    font-size: 13px; color: var(--text-70); line-height: 1.7;
}}
.m-footer {{
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 16px; padding-top: 12px;
    border-top: 1px solid var(--border);
}}
.m-action {{
    display: inline-flex; align-items: center; gap: 4px;
    padding: 7px 16px;
    background: rgba(107,138,253,0.10); color: var(--accent);
    text-decoration: none; border-radius: 8px;
    font-size: 12px; font-weight: 500; transition: background 0.15s;
}}
.m-action:hover {{ background: rgba(107,138,253,0.18); }}
.m-footer-src {{
    font-size: 10px; color: var(--text-35);
}}

/* ═════ Footer ═════ */
.footer {{
    text-align: center; padding: 28px 20px;
    color: var(--text-35); font-size: 11px;
    border-top: 1px solid var(--border); margin-top: 20px;
}}
.footer b {{ color: var(--accent); }}

/* ═════ Empty & Scrollbar ═════ */
.empty {{ text-align: center; padding: 60px 20px; color: var(--text-50); font-size: 13px; }}
::-webkit-scrollbar {{ width: 3px; }}
::-webkit-scrollbar-track {{ background: transparent; }}
::-webkit-scrollbar-thumb {{ background: rgba(255,255,255,0.08); border-radius: 2px; }}
</style>
</head>
<body>

<!-- Header -->
<header class="header">
<div class="header-inner">
    <div class="h-title">📡 AI 早报</div>
    <div class="h-date">{date_str} 周{weekday} · {time_str}</div>
    <div class="search-wrap">
        <input type="text" class="search-box" id="searchBox" placeholder="搜索…" aria-label="搜索">
    </div>
    <div class="h-stats">
        <span><b>{total}</b> 篇</span>
        <span><b>{sources_count}</b> 源</span>
    </div>
</div>
</header>

<!-- Filters -->
<div class="filter-bar" id="filterBar">{filter_html}</div>

<!-- Briefing -->
{briefing_html}

<!-- Cards Grid -->
<div class="grid" id="grid">{cards_html}</div>

<!-- Modal -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
<div class="modal" id="modalContent"></div>
</div>

<!-- Footer -->
<footer class="footer">
    AI 早报 · <b>{sources_count}</b> 个信息源 · <b>{total}</b> 篇资讯 · {date_str}
</footer>

<script>
const __data = {modal_json};
const typeLabels = {{"paper":"学术论文","news":"新闻报道","official":"官方发布","opinion":"观点文章","community":"社区讨论","video":"视频"}};

function openModal(idx) {{
    const a = __data[idx];
    if (!a) return;

    let imgHtml = a.image ? '<img class="m-img" src="' + a.image + '" onerror="this.style.display=\\\'none\\\'" alt="">' : '';

    let metaParts = (a.categories||[]).slice(0,2);
    if (a.source_type) metaParts.push(typeLabels[a.source_type]||a.source_type);
    metaParts.push(a.reading_minutes + ' min');
    let pills = metaParts.join(' <span class="m-pill-sep">·</span> ');

    // 深度解读区 — 卡片上看不到的内容
    let deepHtml = '';
    if (a.background) {{
        deepHtml += '<div class="m-deep-section"><div class="m-deep-label">背景脉络</div><div class="m-deep-text m-bg">' + a.background + '</div></div>';
    }}
    if (a.deep_analysis) {{
        deepHtml += '<div class="m-deep-section"><div class="m-deep-label">深度解读</div><div class="m-deep-text m-da">' + a.deep_analysis + '</div></div>';
    }}
    if (deepHtml) {{
        deepHtml = '<div class="m-deep">' + deepHtml + '</div>';
    }}

    document.getElementById('modalContent').innerHTML =
        '<button class="m-close" onclick="closeModal()">✕</button>' +
        '<div class="m-pills">' + pills + '</div>' +
        imgHtml +
        '<div class="m-title">' + (a.title || a.summary) + '</div>' +
        '<div class="m-summary">' + a.summary + '</div>' +
        deepHtml +
        '<div class="m-footer"><span class="m-footer-src">' + a.source_icon + ' ' + a.source_name + ' · ' + a.pub_date + '</span>' +
        '<a href="' + a.link + '" target="_blank" rel="noopener" class="m-action">阅读原文 →</a></div>';

    document.getElementById('modalOverlay').classList.add('show');
    document.body.style.overflow = 'hidden';
}}

function closeModal() {{
    document.getElementById('modalOverlay').classList.remove('show');
    document.body.style.overflow = '';
}}

// Filters
document.querySelectorAll('.f-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
        document.querySelectorAll('.f-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const f = btn.dataset.filter;
        document.querySelectorAll('.card').forEach(c => {{
            c.classList.toggle('hidden', f !== 'all' && !(c.dataset.cat && c.dataset.cat.includes(f)));
        }});
    }});
}});

// Search
document.getElementById('searchBox').addEventListener('input', function() {{
    const q = this.value.toLowerCase();
    document.querySelectorAll('.card').forEach(c => {{
        c.classList.toggle('hidden', q && !c.textContent.toLowerCase().includes(q));
    }});
}});

// Keyboard
document.addEventListener('keydown', e => {{
    if (e.key === 'Escape') closeModal();
    if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {{
        e.preventDefault();
        document.getElementById('searchBox').focus();
    }}
}});
</script>
</body>
</html>'''

    return html
# 第五部分：主流程
# ═══════════════════════════════════════════════════════════════════════

def _default_analysis(title):
    """生成默认的分析结构（LLM 跳过或未配置时使用）"""
    return {
        'ai_relevant': True,
        'summary': title[:100] if title else '无标题',
        'why_it_matters': '',
        'key_details': [],
        'background': '',
        'deep_analysis': '',
        'importance': 1,
        'is_follow_up': False,
        'categories': ['其他'],
        'source_type': 'news',
        'reading_minutes': 1,
    }


def main():
    script_dir = Path(__file__).parent
    config_path = script_dir / 'config.json'

    print("=" * 55)
    print("  📡 AI 早报 v3 — 实用信息框架版")
    print("=" * 55)

    # 读取配置
    with open(config_path, 'r', encoding='utf-8') as f:
        config = json.load(f)

    sources = config['sources']
    settings = config['settings']
    source_authority = config.get('source_authority', {})
    max_items = settings.get('max_items_per_source', 10)
    max_age = settings.get('max_age_hours', 48)
    skip_llm = '--no-llm' in sys.argv

    all_sources = sources.get('english', []) + sources.get('chinese', [])
    print(f"\n📋 共 {len(all_sources)} 个信息源\n")

    # 初始化源健康度追踪
    health_tracker = SourceHealthTracker(
        health_path=str(script_dir / 'source_health.json'),
        alert_threshold=3
    )

    # ① 并发抓取 RSS
    all_items = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_feed, src, max_items, max_age, health_tracker): src
                   for src in all_sources}
        for future in concurrent.futures.as_completed(futures):
            all_items.extend(future.result())

    # 源健康度报警
    health_tracker.print_report()
    health_tracker.save()

    # 按时间排序
    def sort_key(item):
        if item.get('published'):
            return (0, -item['published'].timestamp())
        return (1, 0)
    all_items.sort(key=sort_key)
    print(f"\n📊 共获取 {len(all_items)} 条资讯")

    # ② 去重（DedupEngine：哈希 + 语义两层去重，SQLite 持久化）
    dedup_db_path = str(script_dir / 'dedup.db')
    dedup_engine = DedupEngine(
        db_path=dedup_db_path,
        semantic_threshold=0.60,
        recent_hours=72
    )
    ranker = SourceRanker(config)
    all_items = dedup_engine.deduplicate(
        all_items,
        source_authority=ranker.authority
    )
    # 标记所有通过去重的条目为"新"（DedupEngine 已处理历史对比）
    for item in all_items:
        item['_is_new'] = True

    # 限制总数
    all_items = all_items[:100]

    # ③-B 关键词预过滤：对 ai_only=false 的源，先用规则排除明显非 AI 内容
    #      （减少 LLM 调用量，实测可省 70%+ API 成本）
    ai_only_sources = set()
    for lang in ('english', 'chinese'):
        for src in config.get('sources', {}).get(lang, []):
            if src.get('ai_only', False):
                ai_only_sources.add(src['name'])

    all_items = _keyword_prefilter(all_items, ai_only_sources)

    # ④ 抓取文章原文（为 LLM 提供更多上下文）
    all_items = enrich_articles_with_content(all_items)

    # ⑤ LLM 分析 + AI 相关性过滤
    digest = {"editorial": "", "top_stories": []}
    if not skip_llm:
        analyzer = create_analyzer_from_config(config)
        if analyzer:
            analyses = analyzer.batch_analyze(all_items)
            for item, analysis in zip(all_items, analyses):
                item['analysis'] = analysis

            # 过滤掉 AI 无关的内容
            before_filter = len(all_items)
            all_items = [i for i in all_items if i.get('analysis', {}).get('ai_relevant', True)]
            filtered_out = before_filter - len(all_items)
            if filtered_out > 0:
                print(f"  🗑️ AI 相关性过滤移除 {filtered_out} 条非 AI 内容")

            # ⑥ 生成今日速览
            print("\n📝 生成今日速览...")
            digest = analyzer.generate_digest(all_items)
        else:
            print("  ⚠️ LLM 未配置，使用基础模式")
            for item in all_items:
                item['analysis'] = _default_analysis(item.get('title', ''))
    else:
        print("\n⏭️ 跳过 LLM 分析（--no-llm）")
        for item in all_items:
            item['analysis'] = _default_analysis(item.get('title', ''))

    # ⑦ 来源信誉评分 + 聚类标注 + 综合排序
    all_items = ranker.score_and_filter(all_items, min_authority=0)
    all_items = ranker.enrich_cluster_info(all_items)
    all_items = ranker.sort_by_relevance(all_items)

    # 清理过期记录 + 关闭去重引擎
    dedup_engine.db.cleanup(keep_days=30)
    dedup_engine.close()

    # ⑨ 生成 HTML
    print("\n🎨 生成页面...")
    html = generate_html(all_items, config, digest)

    output_path = script_dir / settings.get('output_file', 'output/index.html')
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"✅ 页面已生成: {output_path}")
    print(f"📄 文件大小: {output_path.stat().st_size / 1024:.1f} KB")

    # 同时复制一份到上级目录方便预览
    parent_copy = script_dir.parent / 'index.html'
    with open(parent_copy, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"📋 副本: {parent_copy}")

    if '--open' in sys.argv:
        import webbrowser
        webbrowser.open(f'file://{output_path.resolve()}')
        print("🌐 已在浏览器中打开")

    print("\n" + "=" * 55)
    print(f"  ☀️  AI 早报生成完毕！共 {len(all_items)} 条 AI 资讯")
    print("=" * 55)

    # 输出结构化统计供 run_daily.sh 读取
    stats = {
        "article_count": len(all_items),
        "output_file": str(output_path),
        "file_size_kb": round(output_path.stat().st_size / 1024, 1),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    stats_path = script_dir / 'output' / 'stats.json'
    with open(stats_path, 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
