# -*- coding: utf-8 -*-
"""米家设备管家 —— Web 服务（Flask）。"""
import json
import logging
import os
import sys
import threading

from flask import Flask, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import Store                      # noqa: E402
from xiaomi.cloud import MiCloudClient, LoginConfirmNeeded  # noqa: E402
from xiaomi.micloudexception import MiCloudException        # noqa: E402
from xiaomi import miio                     # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("mihome")

app = Flask(__name__, static_folder=os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui"),
    static_url_path="")

store = Store()

cloud_lock = threading.Lock()
cloud = MiCloudClient()


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
    return ok({
        "logged_in": _ensure_login(),
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
            return jsonify({"ok": False, "need_confirm": True,
                            "notif_url": e.notif_url,
                            "error": "小米要求登录确认：请在米家 App 或通知里确认后，点击「我已确认」重试。"})
        except MiCloudException as e:
            return err("登录失败：%s" % e, 401)
        except Exception as e:
            return err("登录异常：%s" % e, 500)
        # 成功
        store.save_xiaomi(username, password)
        if remember:
            store.kv_set("xiaomi.password_plain", password)
        else:
            store.kv_del("xiaomi.password_plain")
        _cloud_state_save()
    devices = _refresh_devices_internal()
    return ok({"username": username, "country": country,
               "devices": len(devices)})


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


@app.post("/api/auth/logout")
def auth_logout():
    store.clear_xiaomi()
    return ok()


# ---------------------------------------------------------------- 设备
def _refresh_devices_internal():
    with cloud_lock:
        try:
            devices = cloud.get_devices()
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
        if not _ensure_login():
            raise RuntimeError("云端登录态失效，请重新登录")
        r = cloud.miot_prop_set(did, params["siid"], params["piid"], params["value"])
    elif atype == "miot_action":
        if not _ensure_login():
            raise RuntimeError("云端登录态失效，请重新登录")
        r = cloud.miot_action(did, params["siid"], params["aiid"], params.get("in"))
    elif atype == "rpc":
        if not _ensure_login():
            raise RuntimeError("云端登录态失效，请重新登录")
        r = cloud.rpc(did, params["method"], params.get("params"))
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
    except (MiCloudException, RuntimeError, miio.MiIOError, OSError) as e:
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
    if not _ensure_login():
        return err("未登录", 401)
    body = request.get_json(silent=True) or {}
    try:
        r = cloud.rpc(body.get("did"), body["method"], body.get("params"))
        return jsonify({"ok": True, "result": r})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    port = int(os.environ.get("MIA_PORT", "28100"))
    log.info("米家设备管家启动 :%s", port)
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
