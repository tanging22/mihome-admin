# -*- coding: utf-8 -*-
"""小米云端客户端。

基于 micloud (MIT, (C) 2020 Sammy Svensson) 增强：
  * serviceLoginAuth2 返回 notifUrl 时抛 LoginConfirmNeeded（米家 App 登录确认 2FA）
  * miotspec/prop-get、prop-set、action 云端控制
  * /home/rpc 旧版 RPC
"""
import json
import hashlib
import logging
import time
import datetime

import requests

from . import miutils
from .miutils import get_session
from .micloudexception import MiCloudAccessDenied, MiCloudException

log = logging.getLogger(__name__)


class LoginConfirmNeeded(Exception):
    """小米要求登录确认（2FA）。携带米家 App 确认通知 URL。"""

    def __init__(self, notif_url, raw=None):
        super().__init__("login confirm needed")
        self.notif_url = notif_url
        self.raw = raw or {}


class LoginSmsNeeded(Exception):
    """小米要求短信/邮箱身份验证（2FA identity 流程）。"""

    def __init__(self, options=None, raw=None):
        super().__init__("login sms verify needed")
        self.options = options or []
        self.raw = raw or {}


class MiCloudClient:
    """小米云登录与云端控制。线程不安全，外部加锁使用。"""

    def __init__(self, username=None, password=None, country="cn"):
        self.user_id = None
        self.service_token = None
        self.ssecurity = None
        self.cuser_id = None
        self.pass_token = None
        self.session = None
        self.failed_logins = 0
        self.username = username
        self.password = password
        self.country = (country or "cn").strip().lower()
        tz = datetime.datetime.now().astimezone().strftime("%z")
        self.timezone = "GMT{}:{}".format(tz[:-2], tz[-2:])
        self.locale = "zh_CN"
        self._pending_sign = None  # 2FA identity 流程的会话签名

    # ---------------------------------------------------------- 基础
    @property
    def logged_in(self):
        return bool(self.user_id and self.service_token and self.ssecurity)

    def set_credentials(self, username, password):
        self.username = username
        self.password = password

    def load_state(self, state):
        """恢复已有登录态（serviceToken 持久化后免重登）。"""
        self.user_id = state.get("user_id")
        self.service_token = state.get("service_token")
        self.ssecurity = state.get("ssecurity")
        self.cuser_id = state.get("cuser_id")
        self.pass_token = state.get("pass_token")
        self.country = state.get("country", self.country)

    def dump_state(self):
        return {
            "user_id": self.user_id,
            "service_token": self.service_token,
            "ssecurity": self.ssecurity,
            "cuser_id": self.cuser_id,
            "pass_token": self.pass_token,
            "country": self.country,
        }

    def _api_url(self):
        c = self.country
        return "https://" + ("" if c == "cn" else c + ".") + "api.io.mi.com/app"

    # ---------------------------------------------------------- 登录
    def login(self):
        """完整登录。需要米家 App 确认时抛 LoginConfirmNeeded。"""
        if not (self.username and self.password):
            raise MiCloudException("需要小米账号与密码")
        if self.logged_in:
            return True
        self._init_session(reset=True)
        sign = self._login_step1()
        location = self._login_step2(sign if isinstance(sign, str) else "")
        resp3 = self._login_step3(location)
        if resp3.status_code == 403:
            raise MiCloudAccessDenied("小米云拒绝访问（403）")
        if resp3.status_code == 200 and self.service_token:
            self.failed_logins = 0
            log.info("小米云登录成功 userId=%s", self.user_id)
            return True
        raise MiCloudException("登录失败: %s %s" % (resp3.status_code, resp3.reason))

    def _init_session(self, reset=False):
        if not self.session or reset:
            if self.session is not None:
                try:
                    self.session.close()
                except Exception:
                    pass
            self.session = get_session()

    def _login_step1(self):
        url = "https://account.xiaomi.com/pass/serviceLogin?sid=xiaomiio&_json=true"
        self.session.cookies.update({"userId": self.username})
        resp = self.session.get(url)
        data = json.loads(resp.text.replace("&&&START&&&", ""))
        return data.get("_sign", "")

    def _login_step2(self, sign):
        url = "https://account.xiaomi.com/pass/serviceLoginAuth2"
        post = {
            "sid": "xiaomiio",
            "hash": hashlib.md5(self.password.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": self.username,
            "_json": "true",
        }
        if sign:
            post["_sign"] = sign
        resp = self.session.post(url, data=post)
        data = json.loads(resp.text.replace("&&&START&&&", ""))
        # --- 2FA: 米家 App 登录确认 ---
        notif = data.get("notifUrl") or data.get("notificationUrl")
        if notif and data.get("result") != "ok":
            raise LoginConfirmNeeded(notif, data)
        if data.get("captcha"):
            raise MiCloudException("小米要求验证码（captcha）: %s" % data.get("captchaUrl", ""))
        if data.get("result") != "ok":
            # 无 notifUrl 的 2FA（短信/邮箱验证）：探测 identity 流程
            options = self._identity_probe(sign)
            if options:
                log.info("小米要求身份验证, options=%s", options)
                raise LoginSmsNeeded(options, data)
            desc = data.get("desc") or json.dumps(data, ensure_ascii=False)[:200]
            raise MiCloudAccessDenied("登录被拒绝: %s" % desc)
        self.user_id = data["userId"]
        self.ssecurity = data["ssecurity"]
        self.cuser_id = data.get("cUserId")
        self.pass_token = data.get("passToken")
        location = data.get("location")
        if not location:
            raise MiCloudException("未返回登录 location，code=%s" % data.get("code"))
        return location

    def _login_step3(self, location):
        self.session.headers.update({"content-type": "application/x-www-form-urlencoded"})
        resp = self.session.get(location)
        service_token = resp.cookies.get("serviceToken")
        if service_token:
            self.service_token = service_token
        return resp

    # ------------------------------------------------------ 2FA 短信验证
    def _identity_probe(self, sign):
        """探测小米 identity 2FA 方式（phone/email）。失败返回 []。"""
        try:
            url = "https://account.xiaomi.com/identity/list"
            resp = self.session.get(url, params={
                "sid": "xiaomiio", "_sign": sign, "_json": "true"})
            data = json.loads(resp.text.replace("&&&START&&&", ""))
            self._pending_sign = data.get("_sign") or sign
            return data.get("options") or []
        except Exception as e:
            log.warning("identity/list 探测失败: %s", e)
            return []

    def sms_send(self):
        """触发短信验证码下发。"""
        if not self._pending_sign:
            raise MiCloudException("没有待验证的登录会话，请先重新提交登录")
        url = "https://account.xiaomi.com/identity/auth/verifyPhone"
        resp = self.session.post(url, params={
            "sid": "xiaomiio", "_sign": self._pending_sign, "sendFirst": "true"},
            data={"sendFirst": "true"})
        try:
            data = json.loads(resp.text.replace("&&&START&&&", ""))
        except Exception:
            raise MiCloudException("发送验证码响应异常: %s" % resp.text[:200])
        code = data.get("code")
        if code not in (0, None, "0"):
            raise MiCloudException("发送验证码失败(code=%s): %s" % (
                code, data.get("desc") or json.dumps(data, ensure_ascii=False)[:150]))
        log.info("短信验证码已下发")
        return True

    def sms_verify(self, ticket):
        """提交短信验证码完成登录。"""
        if not self._pending_sign:
            raise MiCloudException("没有待验证的登录会话，请先重新提交登录")
        url = "https://account.xiaomi.com/identity/auth/verifyPhone"
        resp = self.session.post(url, params={
            "sid": "xiaomiio", "_sign": self._pending_sign},
            data={"ticket": str(ticket).strip()})
        try:
            data = json.loads(resp.text.replace("&&&START&&&", ""))
        except Exception:
            raise MiCloudException("验证码校验响应异常: %s" % resp.text[:200])
        location = data.get("location")
        if not location:
            raise MiCloudException("验证码校验失败: %s" % json.dumps(data, ensure_ascii=False)[:200])
        if data.get("userId"):
            self.user_id = data["userId"]
        self.ssecurity = data.get("ssecurity") or self.ssecurity
        self.cuser_id = data.get("cUserId")
        self.pass_token = data.get("passToken") or self.pass_token
        resp3 = self._login_step3(location)
        if resp3.status_code == 200 and self.service_token and self.ssecurity:
            self.failed_logins = 0
            self._pending_sign = None
            log.info("短信验证登录成功 userId=%s", self.user_id)
            return True
        raise MiCloudException("短信验证后换取 token 失败: %s %s" % (resp3.status_code, resp3.reason))

    # ---------------------------------------------------------- 请求
    def request(self, url_part, params, method="POST"):
        """调用云端加密接口。url_part 形如 /home/device_list。"""
        if not (self.service_token and self.user_id and self.ssecurity):
            raise MiCloudException("未登录，无法调用 %s" % url_part)
        url = self._api_url() + url_part
        self._init_session(reset=True)
        self.session.headers.update({
            "Accept-Encoding": "identity",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "content-type": "application/x-www-form-urlencoded",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        })
        self.session.cookies.update({
            "userId": str(self.user_id),
            "yetAnotherServiceToken": self.service_token,
            "serviceToken": self.service_token,
            "locale": str(self.locale),
            "timezone": str(self.timezone),
            "is_daylight": str(time.daylight),
            "dst_offset": str(time.localtime().tm_isdst * 60 * 60 * 1000),
            "channel": "MI_APP_STORE",
        })
        nonce = miutils.gen_nonce()
        signed_nonce = miutils.signed_nonce(self.ssecurity, nonce)
        post_data = miutils.generate_enc_params(
            url, method, signed_nonce, nonce, params, self.ssecurity)
        resp = self.session.post(url, data=post_data)
        if resp.status_code == 403:
            self.service_token = None
            raise MiCloudException("403 未授权，serviceToken 失效，请重新登录")
        if resp.status_code != 200:
            raise MiCloudException("%s 返回 %s" % (url_part, resp.status_code))
        return miutils.decrypt_rc4(
            miutils.signed_nonce(self.ssecurity, post_data["_nonce"]), resp.text)

    def request_json(self, url_part, params):
        return json.loads(self.request(url_part, params))

    # ---------------------------------------------------------- 设备
    def get_devices(self):
        """设备列表（含 token / ip / miot spec 关系）。"""
        data = {"getVirtualModel": True, "getHuamiDevices": 1,
                "get_split_device": False, "support_smart_home": True}
        resp = self.request_json("/home/device_list", {"data": json.dumps(data)})
        if not resp.get("result"):
            raise MiCloudException("设备列表返回异常: %s" % json.dumps(resp, ensure_ascii=False)[:200])
        return resp["result"].get("list", [])

    # ---------------------------------------------------------- 控制
    def miot_prop_get(self, did, siid, piid):
        params = {"params": [{"did": did, "siid": siid, "piid": piid}]}
        return self.request_json("/miotspec/prop-get", params)

    def batch_prop_get(self, items):
        """批量读属性，items=[{did,siid,piid},...]。"""
        return self.request_json("/miotspec/prop-get", {"params": items})

    def miot_prop_set(self, did, siid, piid, value):
        params = {"params": [{"did": did, "siid": siid, "piid": piid, "value": value}]}
        return self.request_json("/miotspec/prop-set", params)

    def miot_action(self, did, siid, aiid, args=None):
        params = {"params": [{"did": did, "siid": siid, "aiid": aiid,
                              "in": args or []}]}
        return self.request_json("/miotspec/action", params)

    def rpc(self, did, method, params=None):
        """旧版 /home/rpc。"""
        body = {"params": params or [], "id": int(time.time() * 1000) % 100000}
        if did:
            body["did"] = did
        return self.request_json("/home/rpc", body)

    def rpc_all(self, method, params=None):
        return self.rpc(None, method, params)
