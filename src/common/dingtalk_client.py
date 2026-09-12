from __future__ import annotations

import logging
from typing import Any, Dict

import requests


logger = logging.getLogger(__name__)


class DingTalkClientError(RuntimeError):
    pass


class DingTalkClient:
    GET_TOKEN_URL = "https://oapi.dingtalk.com/gettoken"
    GET_USER_BY_MOBILE_URL = "https://oapi.dingtalk.com/topapi/v2/user/getbymobile"
    SEND_MESSAGE_URL = "https://oapi.dingtalk.com/topapi/message/corpconversation/asyncsend_v2"

    def __init__(self, timeout: int = 10) -> None:
        self.timeout = timeout

    def get_access_token(self, app_key: str, app_secret: str) -> str:
        response = requests.get(
            self.GET_TOKEN_URL,
            params={"appkey": app_key, "appsecret": app_secret},
            timeout=self.timeout,
        )
        payload = self._parse_json(response)
        self._raise_for_payload_error(payload, default_message="获取 access_token 失败")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise DingTalkClientError("钉钉未返回 access_token")
        return token

    def get_userid_by_mobile(self, access_token: str, mobile: str) -> str:
        response = requests.post(
            self.GET_USER_BY_MOBILE_URL,
            params={"access_token": access_token},
            json={"mobile": mobile},
            timeout=self.timeout,
        )
        payload = self._parse_json(response)
        self._raise_for_payload_error(payload, default_message="绑定接收人失败")
        result = payload.get("result") or {}
        userid = str(result.get("userid") or "").strip()
        if not userid:
            raise DingTalkClientError("钉钉未返回接收人 userid")
        return userid

    def send_text_message(
        self,
        *,
        access_token: str,
        agent_id: str,
        userid: str,
        content: str,
    ) -> Dict[str, Any]:
        response = requests.post(
            self.SEND_MESSAGE_URL,
            params={"access_token": access_token},
            json={
                "agent_id": int(str(agent_id).strip()),
                "userid_list": userid,
                "to_all_user": False,
                "msg": {
                    "msgtype": "text",
                    "text": {
                        "content": content,
                    },
                },
            },
            timeout=self.timeout,
        )
        payload = self._parse_json(response)
        self._raise_for_payload_error(payload, default_message="发送钉钉提醒失败")
        return {
            "request_id": str(payload.get("request_id") or ""),
            "task_id": str(payload.get("task_id") or ""),
            "errmsg": str(payload.get("errmsg") or "ok"),
        }

    def _parse_json(self, response: requests.Response) -> Dict[str, Any]:
        if response.status_code >= 400:
            raise DingTalkClientError(f"钉钉接口请求失败: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("钉钉接口返回非 JSON: %s", response.text[:300])
            raise DingTalkClientError("钉钉接口返回了无效响应") from exc
        if not isinstance(payload, dict):
            raise DingTalkClientError("钉钉接口返回格式异常")
        return payload

    @staticmethod
    def _raise_for_payload_error(payload: Dict[str, Any], *, default_message: str) -> None:
        errcode = int(payload.get("errcode", 0) or 0)
        errmsg = str(payload.get("errmsg") or "").strip()
        if errcode != 0:
            raise DingTalkClientError(errmsg or default_message)


_dingtalk_client: DingTalkClient | None = None


def get_dingtalk_client() -> DingTalkClient:
    global _dingtalk_client
    if _dingtalk_client is None:
        _dingtalk_client = DingTalkClient()
    return _dingtalk_client
