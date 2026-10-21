# -*- coding: utf-8 -*-
"""米家设备管家 —— Web 服务（Flask）。

访问模型：
  * 统一网关（unix socket，fnOS 校验 NAS 登录态后转发，带 X-Trim-* 头）→ 免面板密码
  * TCP 直连（28100）→ 可在设置中开关监听/绑定地址，设置面板密码后需验证
"""
import hashlib
import json
import logging
import os
import sys
import threading

from flask import Flask, jsonify, request, send_from_directory, make_response

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store                      # noqa: E402
from xiaomi.cloud import (                   # noqa: E402
    MiCloudClient, LoginConfirmNeeded, LoginSmsNeeded)
from xiaomi.micloudexception import MiCloudException        # noqa: E402
from xiaomi import miio                     # noqa: E402
from xiaomi import oauth                    # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mihome")

app = Flask(__name__, static_folder=os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui"),
    static_url_path="")


class _PrefixMiddleware:
    """fnOS 统一网关转发保留 /app/mihome-admin 前缀 → 剥掉再进路由。

    TCP 直连（不带前缀）的请求原样通过，互不影响。
    前缀根不带尾斜杠时 308 重定向到带斜杠版本：iframe URL 无尾斜杠时，
    浏览器会把相对路径 assets/app.js 解析到 /app/assets/（丢失应用段）导致 404。
    """

    def __init__(self, wsgi, prefix):
        self.wsgi = wsgi
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == self.prefix:
            start_response("308 Permanent Redirect", [
                ("Location", self.prefix + "/"),
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", "0"),
            ])
            return [b""]
        if path.startswith(self.prefix + "/"):
            environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.wsgi(environ, start_response)


app.wsgi_app = _PrefixMiddleware(
    app.wsgi_app, os.environ.get("MIA_URL_PREFIX", "/app/mihome-admin"))

store = Store()

cloud_lock = threading.Lock()
cloud = MiCloudClient()

# SMS 登录中间态（username/password/country/remember 待验证通过后落库）
_pending_auth = {}

# OAuth 官方登录态：_oauth 为 xiaomi.oauth.HaClient 或 None
_oauth = None
_pending_oauth = {}     # state -> {"redirect": uri, "ts": epoch}
_oauth_done = set()     # 已完成验证的 state


def _oauth_ready():
    """恢复持久化 OAuth 登录态；有效返回 True。"""
    global _oauth
    if _oauth is not None:
        return True
    st = store.load_cloud_state() or {}
    if st.get("auth_type") == "oauth" and st.get("access_token"):
        _oauth = oauth.HaClient(st["access_token"], st.get("device_id"),
                                st.get("country") or "cn")
        return True
    return False


def _cloud_call(method, *args, **kwargs):
    """云端调用 dispatch：OAuth 官方登录优先，serviceToken 兜底。

    method 形如 "get_devices" / "miot_prop_set"，在两个客户端上同形。
    """
    global _oauth
    if _oauth_ready():
        try:
            return getattr(_oauth, method)(*args, **kwargs)
        except oauth.OAuthTokenExpired:
            _oauth = None          # token 失效，落回 serviceToken 流程
    if not _ensure_login():
        raise RuntimeError("云端登录态失效，请重新登录")
    return getattr(cloud, method)(*args, **kwargs)

# ---------------------------------------------------------------- TCP 监听管理
_tcp_server = None
_tcp_lock = threading.Lock()


def _panel_listen_cfg():
    return {
        "enabled": store.kv_get("panel.listen_enabled") != "0",
        "addr": store.kv_get("panel.listen_addr") or "0.0.0.0",
        "port": int(os.environ.get("MIA_PORT", "28100")),
    }


def _tcp_restart():
    """按配置重启 TCP 监听线程（gunicorn 模式下实时生效）。"""
    global _tcp_server
    with _tcp_lock:
        if _tcp_server is not None:
            try:
                _tcp_server.shutdown()
            except Exception:
                pass
            _tcp_server = None
        cfg = _panel_listen_cfg()
        if os.environ.get("MIA_NO_TCP") == "1":
            return None
        if not cfg["enabled"]:
            log.info("TCP 监听已关闭（仅统一网关可访问）")
            return None
        from werkzeug.serving import make_server
        srv = make_server(cfg["addr"], cfg["port"], app, threaded=True)
        t = threading.Thread(target=srv.serve_forever, daemon=True,
                             name="tcp-listen")
        t.start()
        _tcp_server = srv
        log.info("TCP 监听 %s:%s", cfg["addr"], cfg["port"])
        return srv


def _is_trusted():
    """网关（unix socket / X-Trim-* 头）视为已通过 fnOS 登录校验。"""
    if request.headers.get("X-Trim-Userid"):
        return True
    remote = request.environ.get("REMOTE_ADDR") or ""
    # unix socket 进来的请求没有 IPv4 远端地址
    if remote in ("", "-", None) and not request.remote_addr:
        return True
    return False


def _panel_token():
    h = store.kv_get("panel.password_hash")
    return hashlib.md5((h or "").encode()).hexdigest() if h else None


@app.before_request
def _panel_guard():
    if _is_trusted():
        return None
    # 静态资源（/、/assets/*、/images/*）公开：页面只是壳，数据由 API 保护
    if not request.path.startswith("/api/"):
        return None
    # TCP 直连：免登录端点
    if request.path in ("/api/panel/config", "/api/panel/login") \
            or request.path.startswith("/api/panel/ping"):
        return None
    pw_hash = store.kv_get("panel.password_hash")
    if not pw_hash:          # 未设置面板密码 = 不验证
        return None
    ck = request.cookies.get("mia_panel", "")
    if ck and ck == _panel_token():
        return None
    return jsonify({"ok": False, "need_panel_login": True,
                    "error": "需要面板密码"}), 401


def _cloud_state_save():
    if cloud.logged_in:
        store.save_cloud_state(cloud.dump_state())


def _ensure_login():
    """尝试用持久化登录态恢复；失败返回 False。"""
    global cloud
    with cloud_lock:
        if cloud.logged_in:
            return True
        state = store.load_cloud_state()
        if state:
            cloud.load_state(state)
            if cloud.logged_in:
                return True
        return False


def ok(data=None, **extra):
    d = {"ok": True}
    if data is not None:
        d["data"] = data
    d.update(extra)
    return jsonify(d)


def err(msg, status=400, **extra):
    d = {"ok": False, "error": str(msg)}
    d.update(extra)
    return jsonify(d), status


# ---------------------------------------------------------------- 静态 UI
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------- 状态/登录
@app.get("/api/status")
def status():
    devices, ts = store.load_devices()
    auth_type = "oauth" if _oauth_ready() else ("password" if _ensure_login() else "")
    return ok({
        "logged_in": bool(auth_type),
        "auth_type": auth_type,
        "username": store.get_xiaomi_username(),
        "country": cloud.country,
        "devices_cached": len(devices),
        "devices_ts": ts,
    })


@app.post("/api/auth/login")
def auth_login():
    global cloud
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    country = (body.get("country") or "cn").strip().lower()
    remember = bool(body.get("remember_password"))
    if not username or not password:
        return err("需要账号与密码")
    with cloud_lock:
        cloud = MiCloudClient(username, password, country)
        try:
            cloud.login()
        except LoginConfirmNeeded as e:
            _pending_auth.update(username=username, password=password,
                                 country=country, remember=remember)
            return jsonify({"ok": False, "need_confirm": True,
                            "notif_url": e.notif_url,
                            "error": "小米要求登录确认：请在米家 App 或通知里确认后，点击「我已确认」重试。"})
        except LoginSmsNeeded as e:
            _pending_auth.update(username=username, password=password,
                                 country=country, remember=remember)
            return jsonify({"ok": False, "need_sms": True,
                            "options": e.options,
                            "error": "小米要求短信验证：请获取并输入短信验证码。"})
        except MiCloudException as e:
            return err("登录失败：%s" % e, 401)
        except Exception as e:
            return err("登录异常：%s" % e, 500)
        # 成功
        _auth_success(username, password, country, remember)
    devices = _refresh_devices_internal()
    return ok({"username": username, "country": country,
               "devices": len(devices)})


def _auth_success(username, password, country, remember):
    store.save_xiaomi(username, password)
    if remember:
        store.kv_set("xiaomi.password_plain", password)
    else:
        store.kv_del("xiaomi.password_plain")
    _cloud_state_save()


@app.post("/api/auth/sms/send")
def auth_sms_send():
    """触发短信验证码下发（需先提交过密码登录）。"""
    with cloud_lock:
        try:
            cloud.sms_send()
        except MiCloudException as e:
            return err(str(e), 400)
        except Exception as e:
            return err("发送验证码异常：%s" % e, 500)
    return ok({"sent": True})


@app.post("/api/auth/sms/verify")
def auth_sms_verify():
    """提交短信验证码完成登录。"""
    global cloud
    body = request.get_json(silent=True) or {}
    ticket = (body.get("ticket") or "").strip()
    if not ticket:
        return err("需要短信验证码")
    with cloud_lock:
        try:
            cloud.sms_verify(ticket)
        except MiCloudException as e:
            return err("验证失败：%s" % e, 401)
        except Exception as e:
            return err("验证异常：%s" % e, 500)
        info = dict(_pending_auth)
        _pending_auth.clear()
        _auth_success(info.get("username") or store.get_xiaomi_username() or "",
                      info.get("password") or "",
                      info.get("country") or cloud.country,
                      bool(info.get("remember")))
        username = store.get_xiaomi_username()
    devices = _refresh_devices_internal()
    return ok({"username": username, "devices": len(devices)})


@app.post("/api/auth/confirm")
def auth_confirm():
    """用户已在米家 App 确认，重试登录。"""
    global cloud
    username = store.get_xiaomi_username()
    pw = store.kv_get("xiaomi.password_plain")
    if not username or not pw:
        return err("没有缓存的账号信息，请回到登录页重新输入")
    with cloud_lock:
        cloud = MiCloudClient(username, pw, cloud.country)
        try:
            cloud.login()
        except LoginConfirmNeeded:
            return err("仍未确认，请在米家 App 完成确认后再试", 401)
        except MiCloudException as e:
            return err("登录失败：%s" % e, 401)
        _cloud_state_save()
    devices = _refresh_devices_internal()
    return ok({"devices": len(devices)})


# ---------------------------------------------------------------- OAuth 官方登录
_OAUTH_STATE_TTL = 600  # start → 完成登录的有效期（秒）


def _oauth_page(title, msg):
    return make_response(
        "<!doctype html><meta charset='utf-8'>"
        "<title>%s</title><body style='font-family:sans-serif;"
        "display:flex;align-items:center;justify-content:center;height:95vh'>"
        "<div style='text-align:center'><h2>%s</h2><p style='color:#666'>%s</p>"
        "<p style='color:#999;font-size:13px'>请回到米家设备管家页面查看结果，本页可以关闭。</p>"
        "</div></body>" % (title, title, msg), 200)


@app.post("/api/auth/oauth/start")
def oauth_start():
    """生成小米官方登录跳转 URL。

    redirect_uri 固定用白名单内的 homeassistant.local:8123：官方页登录后
    浏览器跳到该地址（打不开），用户把地址栏 URL 粘贴回 /exchange 换 token。
    """
    import secrets
    import time as _t
    # 过期清理
    now = _t.time()
    for k in [k for k, v in _pending_oauth.items() if now - v.get("ts", 0) > _OAUTH_STATE_TTL]:
        _pending_oauth.pop(k, None)
    state = secrets.token_hex(16)
    # 授权页与换 token 必须用同一个 device_id，否则小米报 invalid authorization code
    device_id = oauth.new_device_id()
    _pending_oauth[state] = {"redirect": oauth.HA_REDIRECT_URL, "ts": now,
                             "device_id": device_id}
    return ok({"url": oauth.login_url(oauth.HA_REDIRECT_URL, state,
                                      device_id=device_id), "state": state})


@app.post("/api/auth/oauth/exchange")
def oauth_exchange():
    """用户粘贴官方页登录后的回调地址（含 code），换 token 并验证。"""
    import time as _t
    from urllib.parse import urlparse, parse_qs
    body = request.get_json(silent=True) or {}
    raw = (body.get("callback_url") or "").strip()
    if not raw:
        return err("请粘贴登录后浏览器地址栏的完整网址")
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    q = parse_qs(urlparse(raw).query)
    code = (q.get("code") or [""])[0]
    state = (q.get("state") or [""])[0]
    if not code:
        return err("粘贴的网址里没有 code，请确认复制的是登录完成后的地址栏网址")
    info = _pending_oauth.get(state)
    if not info:
        return err("登录会话已过期（state 不匹配），请重新点击官方登录")
    try:
        token = oauth.token_by_code(code, oauth.HA_REDIRECT_URL,
                                    device_id=info.get("device_id"))
    except Exception as e:
        log.warning("OAuth 换 token 失败: %s", e)
        return err("获取 token 失败：%s" % e)
    store.save_cloud_state({
        "auth_type": "oauth",
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "device_id": info.get("device_id"),
        "expires_at": int(_t.time()) + int(float(token.get("expires_in") or 0) * 0.9),
        "saved_at": int(_t.time()),
    })
    global _oauth
    _oauth = oauth.HaClient(token["access_token"], info.get("device_id"))
    # 拿用户昵称（失败不阻断）
    nickname = ""
    try:
        nickname = (_oauth.user_profile().get("miliaoNick") or "")
    except Exception as e:
        log.info("获取用户昵称失败: %s", e)
    try:
        devices = _cloud_call("get_devices")
        store.save_devices(devices)
        if nickname:
            store.kv_set("xiaomi.username", nickname)
    except Exception as e:
        log.warning("OAuth token 已保存，但验证拉取设备失败: %s", e)
        return err("token 已保存，但设备验证失败：%s" % e)
    _oauth_done.add(state)
    _pending_oauth.pop(state, None)
    return ok({"username": nickname, "devices": len(devices)})


@app.get("/api/auth/oauth/callback")
def oauth_callback():
    """小米官方登录回调：code 换 token，验证并保存。"""
    import time as _t
    code = request.args.get("code", "")
    state = request.args.get("state", "")
    info = _pending_oauth.get(state)
    if not info:
        return _oauth_page("会话已过期", "登录会话不存在或已超时，请在面板重新发起官方登录。")
    try:
        token = oauth.token_by_code(code, info["redirect"],
                                    device_id=info.get("device_id"))
    except Exception as e:
        _pending_oauth.pop(state, None)
        log.warning("OAuth 换 token 失败: %s", e)
        return _oauth_page("登录失败", "获取 token 失败：%s" % e)
    store.save_cloud_state({
        "auth_type": "oauth",
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "device_id": info.get("device_id"),
        "expires_at": int(_t.time()) + int(float(token.get("expires_in") or 0) * 0.9),
        "saved_at": int(_t.time()),
    })
    global _oauth
    _oauth = oauth.HaClient(token["access_token"], info.get("device_id"))
    # 拿用户昵称（失败不阻断）
    nickname = ""
    try:
        nickname = (_oauth.user_profile().get("miliaoNick") or "")
    except Exception as e:
        log.info("获取用户昵称失败: %s", e)
    try:
        devices = _cloud_call("get_devices")
        store.save_devices(devices)
        if nickname:
            store.kv_set("xiaomi.username", nickname)
    except Exception as e:
        log.warning("OAuth token 已保存，但验证拉取设备失败: %s", e)
        return _oauth_page("登录部分成功",
                           "token 已保存，但设备验证失败：%s。请到面板刷新设备试试。" % e)
    _oauth_done.add(state)
    _pending_oauth.pop(state, None)
    return _oauth_page("登录成功", "已获取小米官方授权并验证通过。")


@app.get("/api/auth/oauth/status")
def oauth_status():
    state = request.args.get("state", "")
    return ok({"done": state in _oauth_done})


@app.post("/api/auth/logout")
def auth_logout():
    global _oauth
    store.clear_xiaomi()
    _oauth = None
    return ok()


# ---------------------------------------------------------------- 面板设置
@app.get("/api/panel/config")
def panel_config():
    """面板访问配置（免鉴权：登录框需要知道密码是否已设置）。"""
    pw_hash = store.kv_get("panel.password_hash")
    cfg = _panel_listen_cfg()
    return ok({
        "password_set": bool(pw_hash),
        "listen_enabled": cfg["enabled"],
        "listen_addr": cfg["addr"],
        "port": cfg["port"],
    })


@app.post("/api/panel/login")
def panel_login():
    body = request.get_json(silent=True) or {}
    password = body.get("password") or ""
    pw_hash = store.kv_get("panel.password_hash")
    if not pw_hash:
        return ok({"nologin": True})   # 未设密码无需验证
    if hashlib.md5(password.encode()).hexdigest() != pw_hash:
        return err("面板密码不正确", 401)
    resp = make_response(ok({"login": True}))
    resp.set_cookie("mia_panel", _panel_token(), max_age=30 * 86400,
                    httponly=True, samesite="Lax")
    return resp


@app.post("/api/panel/password")
def panel_password():
    body = request.get_json(silent=True) or {}
    old = body.get("old") or ""
    new = body.get("new") or ""
    if len(new) < 4:
        return err("新密码至少 4 位")
    pw_hash = store.kv_get("panel.password_hash")
    if pw_hash:
        if hashlib.md5(old.encode()).hexdigest() != pw_hash:
            return err("旧密码不正确", 403)
    store.kv_set("panel.password_hash",
                 hashlib.md5(new.encode()).hexdigest())
    # 旧 cookie 立即失效，重发新 cookie
    resp = make_response(ok({"set": True}))
    resp.set_cookie("mia_panel", _panel_token(), max_age=30 * 86400,
                    httponly=True, samesite="Lax")
    return resp


@app.post("/api/panel/password/clear")
def panel_password_clear():
    """清除面板密码（恢复免验证）。"""
    body = request.get_json(silent=True) or {}
    old = body.get("old") or ""
    pw_hash = store.kv_get("panel.password_hash")
    if pw_hash and hashlib.md5(old.encode()).hexdigest() != pw_hash:
        return err("密码不正确", 403)
    store.kv_del("panel.password_hash")
    resp = make_response(ok({"cleared": True}))
    resp.delete_cookie("mia_panel")
    return resp


@app.post("/api/panel/listen")
def panel_listen():
    """开关 TCP 监听 / 修改绑定地址（实时生效）。"""
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled"))
    addr = (body.get("addr") or "0.0.0.0").strip()
    import re as _re
    if not _re.match(r"^[0-9.]+$", addr):
        return err("地址格式不正确")
    store.kv_set("panel.listen_enabled", "1" if enabled else "0")
    store.kv_set("panel.listen_addr", addr)
    if os.environ.get("MIA_NO_TCP") != "1":
        _tcp_restart()
    return ok({"listen_enabled": enabled, "listen_addr": addr})


# ---------------------------------------------------------------- 设备
def _refresh_devices_internal():
    try:
        devices = _cloud_call("get_devices")
    except Exception as e:
        log.warning("拉取设备列表失败: %s", e)
        devices, _ = store.load_devices()
        return devices
    store.save_devices(devices)
    return devices


@app.get("/api/devices")
def devices():
    refresh = request.args.get("refresh") == "1"
    if refresh:
        if not _ensure_login():
            return err("未登录或登录态失效", 401)
        devices = _refresh_devices_internal()
    else:
        devices, _ = store.load_devices()
    return ok({"devices": devices})


@app.get("/api/devices/<path:model>/spec")
def device_spec(model):
    """按 model 查 miot-spec.org 设备 schema（无需登录）。"""
    import requests as rq
    try:
        # 1. 设备自带 spec_type 优先
        cached, _ = store.load_devices()
        urn = None
        for d in cached:
            if d.get("model") == model:
                urn = d.get("spec_type") or d.get("miot_type")
                break
        # 2. instances 列表按 model 匹配
        if not urn:
            r = rq.get("https://miot-spec.org/miot-spec-v2/instances?status=released",
                       timeout=10)
            for it in r.json().get("instances", []):
                if it.get("model") == model:
                    urn = it.get("type")
                    break
        if not urn:
            return err("找不到 %s 的 spec" % model, 404)
        r = rq.get("https://miot-spec.org/miot-spec-v2/instance?type=%s" % urn,
                   timeout=10)
        return ok({"urn": urn, "spec": r.json()})
    except Exception as e:
        return err("拉取 spec 失败：%s" % e, 502)


# ---------------------------------------------------------------- 栏目
@app.get("/api/sections")
def sections_list():
    return ok({"sections": store.sections()})


@app.post("/api/sections")
def sections_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return err("栏目名不能为空")
    sid = store.section_add(name, sort=int(body.get("sort") or 0))
    return ok({"id": sid})


@app.put("/api/sections/<int:sid>")
def sections_update(sid):
    body = request.get_json(silent=True) or {}
    store.section_update(sid, name=body.get("name"), sort=body.get("sort"))
    return ok()


@app.delete("/api/sections/<int:sid>")
def sections_delete(sid):
    store.section_delete(sid, delete_actions=bool(request.args.get("with_actions", "1") == "1"))
    return ok()


# ---------------------------------------------------------------- 动作
@app.get("/api/actions")
def actions_list():
    return ok({"actions": store.actions(request.args.get("section_id", type=int))})


@app.post("/api/actions")
def actions_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    atype = body.get("action_type")
    if not name or atype not in ("miot_prop_set", "miot_action", "rpc", "miio_lan"):
        return err("需要 name 与合法 action_type")
    aid = store.action_add(
        section_id=body.get("section_id"), name=name,
        device_did=body.get("device_did"), device_name=body.get("device_name"),
        device_model=body.get("device_model"), action_type=atype,
        params=body.get("params") or {},
        confirm=bool(body.get("confirm")), require_password=bool(body.get("require_password")),
        sort=int(body.get("sort") or 0))
    return ok({"id": aid})


@app.put("/api/actions/<int:aid>")
def actions_update(aid):
    body = request.get_json(silent=True) or {}
    store.action_update(aid, **body)
    return ok()


@app.delete("/api/actions/<int:aid>")
def actions_delete(aid):
    store.action_delete(aid)
    return ok()


# ---------------------------------------------------------------- 执行
def _run_action(action, password=None):
    atype = action["action_type"]
    params = action.get("params") or {}
    did = action.get("device_did")
    if atype == "miot_prop_set":
        r = _cloud_call("miot_prop_set", did, params["siid"], params["piid"],
                        params["value"])
    elif atype == "miot_action":
        r = _cloud_call("miot_action", did, params["siid"], params["aiid"],
                        params.get("in"))
    elif atype == "rpc":
        r = _cloud_call("rpc", did, params["method"], params.get("params"))
    elif atype == "miio_lan":
        with socket_ctx() as s:
            r = miio.send(s, params["ip"], params["token"], params["method"],
                          params.get("params"), device_id=params.get("device_id"))
    else:
        raise RuntimeError("未知动作类型 %s" % atype)
    return r


from contextlib import closing            # noqa: E402


def socket_ctx():
    import socket as _s
    return closing(_s.socket(_s.AF_INET, _s.SOCK_DGRAM))


@app.post("/api/actions/<int:aid>/run")
def actions_run(aid):
    action = store.action_get(aid)
    if not action:
        return err("动作不存在", 404)
    body = request.get_json(silent=True) or {}
    if action.get("require_password"):
        pw = body.get("password") or ""
        if not pw:
            return jsonify({"ok": False, "need_password": True,
                            "error": "该动作需要小米安全密码"})
        if not store.check_xiaomi_password(pw):
            return jsonify({"ok": False, "bad_password": True,
                            "error": "安全密码不正确"}), 403
    try:
        r = _run_action(action, body.get("password"))
        return jsonify({"ok": True, "result": r})
    except (MiCloudException, RuntimeError, miio.MiIOError, OSError,
            oauth.OAuthError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---------------------------------------------------------------- 调试直发
@app.post("/api/miio/send")
def miio_send():
    body = request.get_json(silent=True) or {}
    ip = body.get("ip")
    token = body.get("token")
    method = body.get("method")
    if not (ip and token and method):
        return err("需要 ip / token / method")
    try:
        with socket_ctx() as s:
            r = miio.send(s, ip, token, method, body.get("params"),
                          device_id=body.get("device_id"),
                          timeout=float(body.get("timeout") or 4))
        return jsonify({"ok": True, "result": r})
    except (miio.MiIOError, OSError) as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.post("/api/cloud/rpc")
def cloud_rpc():
    body = request.get_json(silent=True) or {}
    try:
        r = _cloud_call("rpc", body.get("did"), body.get("method"), body.get("params"))
        return jsonify({"ok": True, "result": r})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    # 开发模式：直接监听 TCP（不做线程管理，开发够用）
    port = int(os.environ.get("MIA_PORT", "28100"))
    host = store.kv_get("panel.listen_addr") or "0.0.0.0"
    if store.kv_get("panel.listen_enabled") == "0":
        host = "127.0.0.1"
        log.warning("监听已关闭，开发模式仅绑定本机")
    log.info("米家设备管家启动 %s:%s", host, port)
    app.run(host=host, port=port, threaded=True, debug=False)
else:
    # gunicorn 导入模式：按配置起 TCP 监听线程（unix socket 由 gunicorn 管）
    if os.environ.get("MIA_NO_TCP") != "1":
        _tcp_restart()
