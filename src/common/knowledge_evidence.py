import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class KnowledgeEvidenceCollector:
    def __init__(self):
        self._evidence: Dict[str, List[Dict]] = {}

    def record(self, query: str, item_id: str, score: float, source: str = "retrieval"):
        key = query.strip().lower()
        if key not in self._evidence:
            self._evidence[key] = []
        self._evidence[key].append({
            "item_id": item_id,
            "score": score,
            "source": source,
        })

    def get_evidence(self, query: str) -> List[Dict]:
        key = query.strip().lower()
        return self._evidence.get(key, [])

    def clear(self, query: Optional[str] = None):
        if query:
            self._evidence.pop(query.strip().lower(), None)
        else:
            self._evidence.clear()

    def get_all_evidence(self) -> Dict[str, List[Dict]]:
        return dict(self._evidence)


knowledge_evidence_collector = KnowledgeEvidenceCollector()
