"""检索工具底层共享辅助函数（中文分词与消息 ID 提取）。"""

from __future__ import annotations

import re
from typing import Any

CHINESE_STOPWORDS: set[str] = {
    "的", "了", "是", "在", "有", "和", "就", "不", "都", "也", "很", "到", "说", "要",
    "去", "会", "着", "没有", "看", "好", "这", "那", "怎么", "什么", "一下", "可以",
    "请问", "我们", "你们", "他们", "这个", "那个", "还是", "或者", "因为", "所以",
    "如果", "但是", "已经", "目前", "今天", "昨天", "之前", "上次", "还没",
}

_PARTICLE_CHARS = set("的了着过吗呢吧啊呀嘛")


def tokenize_chinese(text: str) -> list[str]:
    """轻量级中文分词实现。
    
    1. 提取按标点和空白切分后的连续语义块；
    2. 对每个连续汉字串进行 Bi-gram（二元滑动窗口）切分（如 '推荐信' -> ['推荐', '荐信', '推荐信']），
       自动跳过首尾含助词的无效跨界 bi-gram（如 '信的'、'的进'）；
    3. 去重且过滤常见虚词（CHINESE_STOPWORDS），避免虚词噪音干扰检索排序。
    """
    if not text:
        return []
    
    # 提取所有由字母、数字、中文构成的连续词块
    raw_blocks = re.findall(r"[\w\u4e00-\u9fa5]+", text.lower())
    tokens: list[str] = []
    
    for block in raw_blocks:
        block = block.strip()
        if not block:
            continue
        # 保留原词（长词）
        tokens.append(block)
        # 对含中文且长度 >= 2 的词段，滑动切出 bi-gram 词元
        if any("\u4e00" <= char <= "\u9fa5" for char in block) and len(block) >= 2:
            for i in range(len(block) - 1):
                bi_gram = block[i:i + 2]
                if bi_gram[0] in _PARTICLE_CHARS or bi_gram[-1] in _PARTICLE_CHARS:
                    continue
                tokens.append(bi_gram)
                
    # 保持顺序并去重
    deduped = list(dict.fromkeys(t for t in tokens if t))
    filtered = [t for t in deduped if t not in CHINESE_STOPWORDS]
    return filtered if filtered else deduped


def collect_message_ids(value: Any) -> set[str]:
    """递归收集字典或列表中包含的全部合法 message_id。"""
    ids: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"message_id"} and isinstance(item, str):
                ids.add(item)
            elif key in {"message_ids", "evidence_message_ids"} and isinstance(item, list):
                ids.update(entry for entry in item if isinstance(entry, str))
            else:
                ids.update(collect_message_ids(item))
    elif isinstance(value, list):
        for item in value:
            ids.update(collect_message_ids(item))
    return ids
