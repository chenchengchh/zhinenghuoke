import jieba


def jaccard_similarity(text_a: str, text_b: str, level: str = "word") -> float:
    if level == "word":
        set_a = set(jieba.cut(text_a))
        set_b = set(jieba.cut(text_b))
    else:
        set_a = set(text_a)
        set_b = set(text_b)
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)
