"""
营销效果追踪服务
追踪消息送达率、打开率、回复率，生成营销报表
"""
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Sequence
from enum import Enum

logger = logging.getLogger(__name__)


class MessageStatus(Enum):
    """消息状态"""
    SENT = "sent"           # 已发送
    DELIVERED = "delivered" # 已送达
    READ = "read"           # 已读
    REPLIED = "replied"     # 已回复
    FAILED = "failed"       # 发送失败


class MarketingTrackingService:
    """营销效果追踪服务"""

    def __init__(self):
        self._message_records: List[dict] = []
        self._campaign_stats: Dict[str, dict] = {}
        self._load_data()

    def _load_data(self):
        """加载数据"""
        try:
            import os
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
            os.makedirs(data_dir, exist_ok=True)
            tracking_file = os.path.join(data_dir, 'marketing_tracking.json')
            if os.path.exists(tracking_file):
                with open(tracking_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self._message_records = data.get('records', [])
                    self._campaign_stats = data.get('stats', {})
        except Exception as e:
            logger.error(f"加载营销追踪数据失败: {e}")

    def _save_data(self):
        """保存数据"""
        try:
            import os
            data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
            os.makedirs(data_dir, exist_ok=True)
            tracking_file = os.path.join(data_dir, 'marketing_tracking.json')
            with open(tracking_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'records': self._message_records,
                    'stats': self._campaign_stats
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存营销追踪数据失败: {e}")

    def record_message(self, customer_name: str, content: str,
                      campaign_id: Optional[str] = None,
                      status: str = "sent",
                      conversation_id: Optional[str] = None,
                      source_message_id: Optional[str] = None,
                      logical_message_id: Optional[str] = None,
                      platform: Optional[str] = None) -> dict:
        """
        记录发送的消息

        Args:
            customer_name: 客户名称
            content: 消息内容
            campaign_id: 营销活动ID（可选）
            status: 消息状态

        Returns:
            消息记录
        """
        record = {
            'id': f"msg_{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
            'customer_name': customer_name,
            'content': content[:100],
            'campaign_id': campaign_id,
            'conversation_id': conversation_id,
            'source_message_id': source_message_id,
            'logical_message_id': logical_message_id,
            'platform': platform,
            'status': status,
            'sent_at': datetime.now().isoformat(),
            'delivered_at': None,
            'read_at': None,
            'replied_at': None
        }

        self._message_records.append(record)

        if campaign_id:
            if campaign_id not in self._campaign_stats:
                self._campaign_stats[campaign_id] = {
                    'total': 0, 'sent': 0, 'delivered': 0, 'read': 0, 'replied': 0, 'failed': 0
                }
            self._campaign_stats[campaign_id]['total'] += 1
            self._campaign_stats[campaign_id][status] = self._campaign_stats[campaign_id].get(status, 0) + 1

        self._save_data()
        return record

    def update_message_status(self, message_id: str, status: str) -> bool:
        """
        更新消息状态

        Args:
            message_id: 消息ID
            status: 新状态

        Returns:
            是否更新成功
        """
        for record in self._message_records:
            if record['id'] == message_id:
                old_status = record['status']
                record['status'] = status

                now = datetime.now().isoformat()
                if status == 'delivered':
                    record['delivered_at'] = now
                elif status == 'read':
                    record['read_at'] = now
                elif status == 'replied':
                    record['replied_at'] = now

                if record.get('campaign_id') and old_status != status:
                    campaign_id = record['campaign_id']
                    if campaign_id in self._campaign_stats:
                        self._campaign_stats[campaign_id][old_status] = max(0, self._campaign_stats[campaign_id].get(old_status, 1) - 1)
                        self._campaign_stats[campaign_id][status] = self._campaign_stats[campaign_id].get(status, 0) + 1

                self._save_data()
                return True

        return False

    def update_latest_message_status(
        self,
        *,
        status: str,
        customer_name: str = "",
        conversation_id: str = "",
        allowed_current_statuses: Optional[Sequence[str]] = None,
    ) -> bool:
        """按客户/会话回溯更新最近一条营销消息状态。"""
        customer_name = (customer_name or "").strip()
        conversation_id = (conversation_id or "").strip()
        allowed_statuses = {item for item in (allowed_current_statuses or []) if item}

        if not customer_name and not conversation_id:
            return False

        for record in reversed(self._message_records):
            if customer_name and record.get("customer_name") != customer_name:
                continue
            if conversation_id and record.get("conversation_id") != conversation_id:
                continue
            if allowed_statuses and record.get("status") not in allowed_statuses:
                continue
            return self.update_message_status(record["id"], status)

        return False

    def get_customer_stats(self, customer_name: str) -> dict:
        """
        获取客户营销统计

        Args:
            customer_name: 客户名称

        Returns:
            客户统计数据
        """
        customer_messages = [m for m in self._message_records if m['customer_name'] == customer_name]

        if not customer_messages:
            return {
                'customer_name': customer_name,
                'total_messages': 0,
                'sent': 0,
                'delivered': 0,
                'read': 0,
                'replied': 0,
                'reply_rate': 0,
                'avg_response_time': None
            }

        total = len(customer_messages)
        delivered = len([m for m in customer_messages if m['status'] in ['delivered', 'read', 'replied']])
        read = len([m for m in customer_messages if m['status'] in ['read', 'replied']])
        replied = len([m for m in customer_messages if m['status'] == 'replied'])

        response_times = []
        for m in customer_messages:
            if m.get('replied_at') and m.get('sent_at'):
                sent = datetime.fromisoformat(m['sent_at'])
                replied_time = datetime.fromisoformat(m['replied_at'])
                response_times.append((replied_time - sent).total_seconds() / 60)

        avg_response = sum(response_times) / len(response_times) if response_times else None

        return {
            'customer_name': customer_name,
            'total_messages': total,
            'delivered': delivered,
            'read': read,
            'replied': replied,
            'reply_rate': round(replied / max(delivered, 1) * 100, 1),
            'avg_response_time': round(avg_response, 1) if avg_response else None
        }

    def get_tracking_summary(self, days: int = 30) -> dict:
        """
        获取追踪汇总

        Args:
            days: 统计天数

        Returns:
            汇总数据
        """
        cutoff = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff.isoformat()

        recent_messages = [m for m in self._message_records if m['sent_at'] >= cutoff_str]

        total = len(recent_messages)
        sent = len([m for m in recent_messages if m['status'] != 'failed'])
        delivered = len([m for m in recent_messages if m['status'] in ['delivered', 'read', 'replied']])
        read = len([m for m in recent_messages if m['status'] in ['read', 'replied']])
        replied = len([m for m in recent_messages if m['status'] == 'replied'])
        failed = len([m for m in recent_messages if m['status'] == 'failed'])

        unique_customers = len(set(m['customer_name'] for m in recent_messages))

        daily_stats = {}
        for m in recent_messages:
            date = m['sent_at'][:10]
            if date not in daily_stats:
                daily_stats[date] = {'sent': 0, 'replied': 0}
            daily_stats[date]['sent'] += 1
            if m['status'] == 'replied':
                daily_stats[date]['replied'] += 1

        return {
            'period_days': days,
            'total_messages': total,
            'unique_customers': unique_customers,
            'delivered': delivered,
            'read': read,
            'replied': replied,
            'failed': failed,
            'delivered_rate': round(delivered / max(total, 1) * 100, 1),
            'read_rate': round(read / max(delivered, 1) * 100, 1),
            'reply_rate': round(replied / max(delivered, 1) * 100, 1),
            'daily_stats': daily_stats
        }

    def get_top_customers(self, limit: int = 10, sort_by: str = "replied") -> List[dict]:
        """
        获取营销效果最好的客户

        Args:
            limit: 返回数量
            sort_by: 排序字段 (replied, read, total)

        Returns:
            客户列表
        """
        customer_stats = {}

        for m in self._message_records:
            customer = m['customer_name']
            if customer not in customer_stats:
                customer_stats[customer] = {'total': 0, 'delivered': 0, 'read': 0, 'replied': 0}

            customer_stats[customer]['total'] += 1
            if m['status'] in ['delivered', 'read', 'replied']:
                customer_stats[customer]['delivered'] += 1
            if m['status'] in ['read', 'replied']:
                customer_stats[customer]['read'] += 1
            if m['status'] == 'replied':
                customer_stats[customer]['replied'] += 1

        sorted_customers = sorted(
            customer_stats.items(),
            key=lambda x: x[1].get(sort_by, 0),
            reverse=True
        )

        return [
            {
                'customer_name': name,
                'total': stats['total'],
                'delivered': stats['delivered'],
                'read': stats['read'],
                'replied': stats['replied'],
                'reply_rate': round(stats['replied'] / max(stats['delivered'], 1) * 100, 1)
            }
            for name, stats in sorted_customers[:limit]
        ]


marketing_tracking_service = MarketingTrackingService()
