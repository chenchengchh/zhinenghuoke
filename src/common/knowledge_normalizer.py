import re
import unicodedata
from typing import Optional


class KnowledgeNormalizer:
    @staticmethod
    def normalize_question(question: str) -> str:
        if not question:
            return ""
        text = question.strip()
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", " ", text)
        text = text.rstrip("？?！!。.，,")
        return text

    @staticmethod
    def normalize_answer(answer: str) -> str:
        if not answer:
            return ""
        text = answer.strip()
        text = unicodedata.normalize("NFKC", text)
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def normalize_category(category: str) -> str:
        if not category:
            return "other"
        return category.strip().lower()

    @staticmethod
    def normalize_item(item) -> dict:
        return {
            "question": KnowledgeNormalizer.normalize_question(getattr(item, "question", "")),
            "answer": KnowledgeNormalizer.normalize_answer(getattr(item, "answer", "")),
            "category": KnowledgeNormalizer.normalize_category(getattr(item, "category", "other")),
        }


knowledge_normalizer = KnowledgeNormalizer()
