# -*- coding: utf-8 -*-
"""SQLite 存储层：凭据、设备缓存、栏目、动作。"""
import hashlib
import json
import os
import sqlite3
import threading
import time

_lock = threading.Lock()


def _default_db_path():
    base = os.environ.get("MIA_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "mihome.db")


class Store:
    def __init__(self, db_path=None):
        self.db_path = db_path or _default_db_path()
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self):
        with _lock, self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, ts INTEGER);
            CREATE TABLE IF NOT EXISTS sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sort INTEGER DEFAULT 0,
                created_at INTEGER);
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER,
                name TEXT NOT NULL,
                device_did TEXT,
                device_name TEXT,
                device_model TEXT,
                action_type TEXT NOT NULL,
                params TEXT DEFAULT '{}',
                confirm INTEGER DEFAULT 0,
                require_password INTEGER DEFAULT 0,
                sort INTEGER DEFAULT 0,
                created_at INTEGER);
            """)

    # ------------------------------------------------------ kv（凭据/登录态）
    def kv_get(self, key, default=None):
        with _lock, self._conn() as c:
            row = c.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default

    def kv_set(self, key, value):
        with _lock, self._conn() as c:
            c.execute("INSERT INTO kv(k,v,ts) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, ts=excluded.ts",
                      (key, value, int(time.time())))

    def kv_del(self, key):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM kv WHERE k=?", (key,))

    # ------------------------------------------------------ 小米凭据
    def save_xiaomi(self, username, password):
        """账号 + 密码 md5（本地安全密码校验用）。"""
        self.kv_set("xiaomi.username", username)
        self.kv_set("xiaomi.password_md5", hashlib.md5(password.encode()).hexdigest().upper())

    def get_xiaomi_username(self):
        return self.kv_get("xiaomi.username")

    def get_xiaomi_password_md5(self):
        return self.kv_get("xiaomi.password_md5")

    def check_xiaomi_password(self, password):
        md5 = self.get_xiaomi_password_md5()
        return bool(md5) and hashlib.md5(password.encode()).hexdigest().upper() == md5

    def clear_xiaomi(self):
        for k in ("xiaomi.username", "xiaomi.password_md5",
                  "xiaomi.cloud_state", "xiaomi.devices"):
            self.kv_del(k)

    def save_cloud_state(self, state: dict):
        self.kv_set("xiaomi.cloud_state", json.dumps(state))

    def load_cloud_state(self):
        raw = self.kv_get("xiaomi.cloud_state")
        return json.loads(raw) if raw else None

    def save_devices(self, devices: list):
        self.kv_set("xiaomi.devices", json.dumps(devices, ensure_ascii=False))
        self.kv_set("xiaomi.devices_ts", str(int(time.time())))

    def load_devices(self):
        raw = self.kv_get("xiaomi.devices")
        ts = self.kv_get("xiaomi.devices_ts")
        return (json.loads(raw), int(ts or 0)) if raw else ([], 0)

    # ------------------------------------------------------ 栏目
    def sections(self):
        with _lock, self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM sections ORDER BY sort, id")]

    def section_add(self, name, sort=0):
        with _lock, self._conn() as c:
            cur = c.execute("INSERT INTO sections(name,sort,created_at) VALUES(?,?,?)",
                            (name, sort, int(time.time())))
            return cur.lastrowid

    def section_update(self, sid, name=None, sort=None):
        with _lock, self._conn() as c:
            if name is not None:
                c.execute("UPDATE sections SET name=? WHERE id=?", (name, sid))
            if sort is not None:
                c.execute("UPDATE sections SET sort=? WHERE id=?", (sort, sid))

    def section_delete(self, sid, delete_actions=True):
        with _lock, self._conn() as c:
            if delete_actions:
                c.execute("DELETE FROM actions WHERE section_id=?", (sid,))
            c.execute("DELETE FROM sections WHERE id=?", (sid,))

    # ------------------------------------------------------ 动作
    def actions(self, section_id=None):
        with _lock, self._conn() as c:
            if section_id is None:
                rows = c.execute("SELECT * FROM actions ORDER BY sort, id").fetchall()
            else:
                rows = c.execute("SELECT * FROM actions WHERE section_id=? ORDER BY sort, id",
                                 (section_id,)).fetchall()
            return [self._action_dict(r) for r in rows]

    @staticmethod
    def _action_dict(r):
        d = dict(r)
        d["params"] = json.loads(d.get("params") or "{}")
        d["confirm"] = bool(d["confirm"])
        d["require_password"] = bool(d["require_password"])
        return d

    def action_add(self, section_id, name, device_did, device_name, device_model,
                   action_type, params, confirm, require_password, sort=0):
        with _lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO actions(section_id,name,device_did,device_name,device_model,"
                "action_type,params,confirm,require_password,sort,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (section_id, name, device_did, device_name, device_model, action_type,
                 json.dumps(params or {}, ensure_ascii=False), int(bool(confirm)),
                 int(bool(require_password)), sort, int(time.time())))
            return cur.lastrowid

    def action_update(self, aid, **fields):
        allowed = {"section_id", "name", "device_did", "device_name", "device_model",
                   "action_type", "params", "confirm", "require_password", "sort"}
        sets, vals = [], []
        for k, v in fields.items():
            if k not in allowed or v is None:
                continue
            if k == "params":
                v = json.dumps(v, ensure_ascii=False)
            if k in ("confirm", "require_password"):
                v = int(bool(v))
            sets.append("%s=?" % k)
            vals.append(v)
        if not sets:
            return
        vals.append(aid)
        with _lock, self._conn() as c:
            c.execute("UPDATE actions SET %s WHERE id=?" % ",".join(sets), vals)

    def action_delete(self, aid):
        with _lock, self._conn() as c:
            c.execute("DELETE FROM actions WHERE id=?", (aid,))

    def action_get(self, aid):
        with _lock, self._conn() as c:
            r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
            return self._action_dict(r) if r else None
