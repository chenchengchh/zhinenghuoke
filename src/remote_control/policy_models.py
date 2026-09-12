from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class RemoteControlPolicy(BaseModel):
    policy_id: str
    instance_id: str = ""
    issued_at: datetime
    expire_at: Optional[datetime] = None
    app_enabled: bool = True
    crawler_enabled: bool = True
    monitor_enabled: bool = True
    auto_reply_enabled: bool = True
    message: str = ""
    signature: str = ""


class RemoteControlSnapshot(BaseModel):
    enabled: bool
    source: str = "default"
    valid: bool
    reason: str = ""
    app_enabled: bool = True
    crawler_enabled: bool = True
    monitor_enabled: bool = True
    auto_reply_enabled: bool = True
    policy_id: str = ""
    instance_id: str = ""
    message: str = ""
    expire_at: Optional[str] = None
    last_refresh_at: Optional[str] = None
