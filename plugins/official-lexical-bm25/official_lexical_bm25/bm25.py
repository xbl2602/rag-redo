"""BM25 + jieba 中文分词的词法检索。

不引入 rank_bm25 这类第三方包——BM25 算法本身只有几十行标准公式，没必要
为此多背一个依赖。jieba 是唯一真正需要的第三方依赖，用来正确切分中文：
纯空白分词对中文没有词间空格，召回会很差——这是旧 obsidian-rag 项目自己
踩过、写进 requirements.txt 注释里的原话教训："缺失时 retriever 会降级
为纯2-gram，召回略降"。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from core.atomic import atomic_write_text

import jieba

INDEXER_VERSION = "0.1.0"

_ENGLISH_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """混合中英文分词：jieba 切中文，纯 ASCII 片段统一转小写（英文检索不
    区分大小写）。标点/空白类切分结果丢弃，不进索引。"""
    tokens: list[str] = []
    for piece in jieba.cut(text):
        piece = piece.strip()
        if not piece:
            continue
        if _ENGLISH_WORD_RE.fullmatch(piece):
            tokens.append(piece.lower())
        elif re.search(r"[一-鿿]", piece):
            tokens.append(piece)
    return tokens


@dataclass
class _Posting:
    doc_id: str
    term_freq: int


@dataclass
class BM25Index:
    """标准 Okapi BM25，k1=1.5、b=0.75——和 Elasticsearch/Lucene 的默认值
    一致，没有特殊理由偏离业界常用默认。"""

    k1: float = 1.5
    b: float = 0.75
    _postings: dict[str, list[_Posting]] = field(default_factory=lambda: defaultdict(list))
    _doc_lengths: dict[str, int] = field(default_factory=dict)
    _doc_tokens_cache: dict[str, Counter] = field(default_factory=dict)

    @property
    def doc_count(self) -> int:
        return len(self._doc_lengths)

    @property
    def _avg_doc_length(self) -> float:
        if not self._doc_lengths:
            return 0.0
        return sum(self._doc_lengths.values()) / len(self._doc_lengths)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._doc_lengths:
            self.remove(doc_id)
        tokens = tokenize(text)
        counts = Counter(tokens)
        self._doc_lengths[doc_id] = len(tokens)
        self._doc_tokens_cache[doc_id] = counts
        for term, freq in counts.items():
            self._postings[term].append(_Posting(doc_id=doc_id, term_freq=freq))

    def remove(self, doc_id: str) -> None:
        if doc_id not in self._doc_lengths:
            return
        counts = self._doc_tokens_cache.pop(doc_id)
        del self._doc_lengths[doc_id]
        for term in counts:
            self._postings[term] = [p for p in self._postings[term] if p.doc_id != doc_id]
            if not self._postings[term]:
                del self._postings[term]

    def _idf(self, term: str) -> float:
        n_docs_with_term = len(self._postings.get(term, []))
        if n_docs_with_term == 0:
            return 0.0
        # 标准 BM25 idf 公式（带 +1 平滑，避免出现在超过半数文档里的词
        # 算出负分）。
        return math.log(1 + (self.doc_count - n_docs_with_term + 0.5) / (n_docs_with_term + 0.5))

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        if self.doc_count == 0:
            return []
        query_terms = tokenize(query)
        scores: dict[str, float] = defaultdict(float)
        avg_len = self._avg_doc_length
        for term in query_terms:
            idf = self._idf(term)
            if idf == 0.0:
                continue
            for posting in self._postings.get(term, []):
                doc_len = self._doc_lengths[posting.doc_id]
                length_norm = (1 - self.b + self.b * doc_len / avg_len) if avg_len else 1.0
                denom = posting.term_freq + self.k1 * length_norm
                scores[posting.doc_id] += idf * (posting.term_freq * (self.k1 + 1)) / denom
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:top_k]

    def to_dict(self) -> dict:
        """只存 doc_lengths + doc_tokens_cache，_postings 是它俩的倒排
        索引视图，重建时重新推导即可，不用重复存一份容易和原数据对不上
        的冗余状态。返回值是纯 JSON 兼容类型（字符串/数字的字典），供
        save() 落盘，也供 official-import-export 插件直接拿去打包进
        导出归档，不需要先写一个临时文件再读回来。"""
        return {
            "k1": self.k1,
            "b": self.b,
            "doc_lengths": dict(self._doc_lengths),
            "doc_tokens_cache": {doc_id: dict(counts) for doc_id, counts in self._doc_tokens_cache.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Index":
        idx = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        idx._doc_lengths = dict(data.get("doc_lengths", {}))
        idx._doc_tokens_cache = {
            doc_id: Counter(counts) for doc_id, counts in data.get("doc_tokens_cache", {}).items()
        }
        for doc_id, counts in idx._doc_tokens_cache.items():
            for term, freq in counts.items():
                idx._postings[term].append(_Posting(doc_id=doc_id, term_freq=freq))
        return idx

    def save(self, path: Path) -> None:
        """用 JSON 不用 pickle——这份数据完全是简单类型，JSON 够用且没有
        反序列化任意代码执行的隐患，没有理由为了省几行代码换一个有安全
        面的格式。"""
        # 原子写（core/atomic.py）：BM25 索引半截 = 词法检索路整体失效
        atomic_write_text(path, json.dumps(self.to_dict(), ensure_ascii=False))

    @classmethod
    def load(cls, path: Path) -> "BM25Index":
        """文件不存在或损坏都安全降级成空索引，不崩溃——同
        AGENTS.md"失败折叠成诚实终态"纪律；空索引意味着这个库下一次
        reindex_knowledge 之前搜不到东西，比进程直接崩溃或读到一份
        损坏数据继续跑安全得多。"""
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)
