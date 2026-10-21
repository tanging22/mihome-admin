# -*- coding: utf-8 -*-
"""小米官方 OAuth2 登录 + Bearer 认证。

协议依据小米官方 Home Assistant 集成 ha_xiaomi_home
（XiaoMi/ha_xiaomi_home miot_cloud.py / const.py，v0.5.0）：
- 授权页: https://account.xiaomi.com/oauth2/authorize
    params: redirect_uri, client_id, response_type=code, device_id, state,
            skip_confirm=false
- 换 token: GET https://ha.api.io.mi.com/app/v2/ha/oauth/get_token
    params: data=json({client_id, redirect_uri, code, device_id})
    响应: {code:0, result:{access_token, refresh_token, expires_in, ...}}
- API 调用: https://ha.api.io.mi.com（非 cn: {cc}.ha.api.io.mi.com）
    头: Authorization: Bearer{access_token}（Bearer 后无空格）
        X-Client-BizId: haapi
        X-Client-AppId: {client_id}
        Content-Type: application/json（POST 为 JSON body，GET 为 query）
    响应: {code:0, result:...}；HTTP 401 = token 失效
- 路径: /app/v2/homeroom/get_dev_room_page（家/房间 → dids）
        /app/v2/home/device_list_page（dids → 设备列表）
        /app/v2/miotspec/prop/get | prop/set | action
- 用户昵称: GET https://open.account.xiaomi.com/user/profile
    params: clientId, token → {code:0, data:{miliaoNick, ...}}
"""
import json
import secrets
import urllib.parse

import requests

OAUTH2_CLIENT_ID = "2882303761520251711"
OAUTH2_AUTH_URL = "https://account.xiaomi.com/oauth2/authorize"
OAUTH2_API_HOST = "ha.api.io.mi.com"
# 官方 client_id 的 redirect_uri 白名单（实测：homeassistant.local:8123 可过，
# 自定义域名返回 96010 invalid redirect uri）。用户在官方页登录后，
# 浏览器会跳到该地址（打不开没关系），把地址栏 URL 粘贴回面板换 token。
HA_REDIRECT_URL = "http://homeassistant.local:8123"
# UA 对齐官方 HA 集成（ha_xiaomi_home/{ver} {system_info} client/{device_id}）
INTEGRATION_VERSION = "v2.0.5"
SYSTEM_INFO = "HA-CORE/2025.1.4 Python/3.13 Linux/x86_64"


def ua_for(device_id):
    return ("ha_xiaomi_home/%s %s client/%s"
            % (INTEGRATION_VERSION, SYSTEM_INFO, device_id))


class OAuthError(Exception):
    pass


class OAuthTokenExpired(OAuthError):
    """access_token 失效（HTTP 401），可重新授权。"""


def new_device_id():
    """官方格式 ha.{uuid4}（MIoTOauthClient: self._device_id = f'ha.{uuid}'）。"""
    import uuid as _u
    return "ha." + str(_u.uuid4())


def make_state(device_id):
    """官方 state 规则：SHA1('d=' + device_id)（miot_cloud.py:111-112）。"""
    import hashlib as _h
    return _h.sha1(("d=" + device_id).encode("utf-8")).hexdigest()


def login_url(redirect_uri, state, device_id=None):
    device_id = device_id or new_device_id()
    q = urllib.parse.urlencode({
        "redirect_uri": redirect_uri,
        "client_id": OAUTH2_CLIENT_ID,
        "response_type": "code",
        "device_id": device_id,
        "state": state,
        "skip_confirm": False,     # 官方默认 False（urlencode 输出 False）
    })
    return "%s?%s" % (OAUTH2_AUTH_URL, q)


def token_by_code(code, redirect_uri, device_id=None, country="cn"):
    """code 换 token，返回 result dict（access_token/refresh_token/expires_in）。"""
    device_id = device_id or new_device_id()
    host = OAUTH2_API_HOST if country == "cn" else "%s.%s" % (country, OAUTH2_API_HOST)
    r = requests.get(
        "https://%s/app/v2/ha/oauth/get_token" % host,
        params={"data": json.dumps({
            "client_id": OAUTH2_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "code": code,
            "device_id": device_id,
        }, separators=(",", ":"))},
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": ua_for(device_id)},
        timeout=20)
    if r.status_code != 200:
        raise OAuthError("get_token 返回 HTTP %s: %s" % (r.status_code, r.text[:200]))
    try:
        body = r.json()
    except ValueError:
        raise OAuthError("get_token 响应无法解析: %s" % r.text[:200])
    if body.get("code") != 0 or "result" not in body:
        raise OAuthError("get_token 失败: %s" % json.dumps(body, ensure_ascii=False)[:300])
    result = body["result"]
    if not result.get("access_token"):
        raise OAuthError("get_token 缺少 access_token: %s"
                         % json.dumps(result, ensure_ascii=False)[:200])
    result["device_id"] = device_id
    return result


class HaClient:
    """官方 OAuth token 的云端客户端（与 MiCloudClient 同形接口）。"""

    def __init__(self, access_token, device_id=None, country="cn"):
        self.access_token = access_token
        self.device_id = device_id or new_device_id()
        self.country = country
        self.host = (OAUTH2_API_HOST if country == "cn"
                     else "%s.%s" % (country, OAUTH2_API_HOST))
        self._s = requests.Session()
        self._s.headers.update(self._headers())

    def _headers(self):
        return {
            "X-Client-BizId": "haapi",
            "X-Client-AppId": OAUTH2_CLIENT_ID,
            "Content-Type": "application/json",
            "Authorization": "Bearer%s" % self.access_token,   # Bearer 后无空格
            "User-Agent": ua_for(self.device_id),
        }

    def _api(self, url_path, data=None, params=None):
        if data is not None:
            r = self._s.post("https://%s%s" % (self.host, url_path),
                             json=data, timeout=20)
        else:
            r = self._s.get("https://%s%s" % (self.host, url_path),
                            params=params, timeout=20)
        if r.status_code == 401:
            raise OAuthTokenExpired("401 未授权，access_token 失效，请重新登录")
        if r.status_code != 200:
            raise OAuthError("%s 返回 HTTP %s: %s"
                             % (url_path, r.status_code, r.text[:200]))
        try:
            body = r.json()
        except ValueError:
            raise OAuthError("%s 响应无法解析: %s" % (url_path, r.text[:200]))
        if body.get("code") not in (0, "0"):
            raise OAuthError("%s 失败: %s" % (url_path,
                                              json.dumps(body, ensure_ascii=False)[:300]))
        return body

    def user_profile(self):
        """用户昵称等信息。"""
        r = requests.get(
            "https://open.account.xiaomi.com/user/profile",
            params={"clientId": OAUTH2_CLIENT_ID, "token": self.access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": ua_for(device_id)},
            timeout=15)
        try:
            body = r.json()
        except ValueError:
            raise OAuthError("profile 响应无法解析: %s" % r.text[:200])
        if body.get("code") != 0 or "data" not in body:
            raise OAuthError("profile 失败: %s"
                             % json.dumps(body, ensure_ascii=False)[:200])
        return body["data"]

    # ---------------------------------------------------------- 设备
    def get_devices(self):
        """设备列表：家/房间 → dids → device_list_page（分页）。"""
        dids: list = []
        start_id = None
        for _ in range(10):                       # 家/房间分页，最多 10 页
            body = self._api("/app/v2/homeroom/get_dev_room_page",
                             data={"start_id": start_id, "limit": 150})
            result = body.get("result") or {}
            for home in result.get("info") or []:
                dids.extend(home.get("dids") or [])
                for room in home.get("roomlist") or []:
                    dids.extend(room.get("dids") or [])
            if result.get("has_more") and result.get("max_id"):
                start_id = result["max_id"]
            else:
                break
        devices: list = []
        start_did = None
        for _ in range(20):                       # 设备分页，每页 200
            data = {"limit": 200, "get_split_device": True,
                    "get_third_device": True, "dids": dids}
            if start_did:
                data["start_did"] = start_did
            body = self._api("/app/v2/home/device_list_page", data=data)
            result = body.get("result") or {}
            devices.extend(result.get("list") or [])
            if result.get("has_more") and result.get("start_did"):
                start_did = result["start_did"]
            else:
                break
        return devices

    # ---------------------------------------------------------- 控制（同形）
    def miot_prop_get(self, did, siid, piid):
        body = self._api("/app/v2/miotspec/prop/get",
                         data={"datasource": 1,
                               "params": [{"did": did, "siid": siid, "piid": piid}]})
        return {"result": body.get("result")}

    def miot_prop_set(self, did, siid, piid, value):
        body = self._api("/app/v2/miotspec/prop/set",
                         data={"params": [{"did": did, "siid": siid,
                                           "piid": piid, "value": value}]},
                         )
        return {"result": body.get("result")}

    def miot_action(self, did, siid, aiid, args=None):
        # NOTICE: 官方协议 action 的 params 是 dict（非标准列表）
        body = self._api("/app/v2/miotspec/action",
                         data={"params": {"did": did, "siid": siid,
                                          "aiid": aiid, "in": args or []}})
        return {"result": body.get("result")}

    def rpc(self, did, method, params=None):
        raise OAuthError("OAuth 登录态不支持旧版 /home/rpc，请使用 miot 动作")

    def rpc_all(self, method, params=None):
        raise OAuthError("OAuth 登录态不支持旧版 /home/rpc，请使用 miot 动作")
