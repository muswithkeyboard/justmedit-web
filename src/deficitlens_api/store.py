from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

log = logging.getLogger("deficitlens.store")

DB_NAME = "justmedit.db"
KEY_NAME = "key"
LINK_TTL = 48 * 3600
SHARE_DAYS = (1, 7, 30, 90)
MAX_ACTIVE_SHARES = 50
MAX_LAB_SETS = 20
MAX_LOGINS = 20
MAX_NAMED_TOKENS = 20
USER_QUOTA_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
ACCESS_LOG_LIMIT = 500
ACCESS_LOG_MERGE = 3600
SIGNUP_WINDOW = 86400
VACUUM_STEP_PAGES = 64
VACUUM_MAX_STEPS = 256
TOUCH_EVERY = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  login       TEXT NOT NULL,
  login_key   TEXT NOT NULL UNIQUE,
  role        TEXT NOT NULL CHECK (role IN ('patient', 'doctor')),
  pw_hash     TEXT NOT NULL,
  clinic      TEXT,
  consent_at  INTEGER NOT NULL,
  created     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  token_sha256 TEXT NOT NULL UNIQUE,
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL CHECK (kind IN ('cookie', 'api')),
  name         TEXT,
  parent_id    TEXT,
  created      INTEGER NOT NULL,
  expires      INTEGER NOT NULL,
  last_used    INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id, kind);
CREATE TABLE IF NOT EXISTS records (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  date           TEXT NOT NULL,
  source         TEXT NOT NULL,
  enc_payload    BLOB NOT NULL,
  engine_version TEXT NOT NULL,
  enc_summary    BLOB NOT NULL,
  created        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS records_user ON records(user_id, date);
CREATE TABLE IF NOT EXISTS shares (
  id          TEXT PRIMARY KEY,
  patient_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doctor_id   TEXT REFERENCES users(id) ON DELETE CASCADE,
  link_sha256 TEXT UNIQUE,
  days        INTEGER NOT NULL,
  created     INTEGER NOT NULL,
  accepted    INTEGER,
  expires     INTEGER NOT NULL,
  revoked     INTEGER,
  enc_doctor  BLOB
);
CREATE INDEX IF NOT EXISTS shares_patient ON shares(patient_id);
CREATE INDEX IF NOT EXISTS shares_doctor ON shares(doctor_id);
CREATE TABLE IF NOT EXISTS access_log (
  id         INTEGER PRIMARY KEY,
  patient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  doctor_id  TEXT REFERENCES users(id) ON DELETE SET NULL,
  what       TEXT NOT NULL CHECK (what IN ('history', 'record', 'records')),
  record_id  TEXT,
  at         INTEGER NOT NULL,
  enc_doctor BLOB
);
CREATE INDEX IF NOT EXISTS access_log_patient ON access_log(patient_id, at);
CREATE TABLE IF NOT EXISTS lab_refs (
  id         TEXT PRIMARY KEY,
  doctor_id  TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT NOT NULL,
  enc_ranges BLOB NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0,
  created    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lab_refs_doctor ON lab_refs(doctor_id);
"""

def _migrate(conn: sqlite3.Connection) -> None:
    _migrate_access_log_check(conn)
    for table in ("access_log", "shares"):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "enc_doctor" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN enc_doctor BLOB")

def _migrate_access_log_check(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'access_log'").fetchone()
    if row is None or "'records'" in (row[0] or ""):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE access_log_new (id INTEGER PRIMARY KEY, "
                     "patient_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                     "doctor_id TEXT REFERENCES users(id) ON DELETE SET NULL, "
                     "what TEXT NOT NULL CHECK (what IN ('history', 'record', 'records')), "
                     "record_id TEXT, at INTEGER NOT NULL)")
        conn.execute("INSERT INTO access_log_new (id, patient_id, doctor_id, what, record_id, at) "
                     "SELECT id, patient_id, doctor_id, what, record_id, at FROM access_log")
        conn.execute("DROP TABLE access_log")
        conn.execute("ALTER TABLE access_log_new RENAME TO access_log")
        conn.execute("CREATE INDEX IF NOT EXISTS access_log_patient ON access_log(patient_id, at)")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

class StoreError(Exception):

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

class LoginTaken(StoreError):
    def __init__(self):
        super().__init__("login_taken", "Этот логин уже занят.")

class TooManyRecords(StoreError):
    def __init__(self, limit: int):
        super().__init__("too_many_records", f"В кабинете можно хранить не больше {limit} анализов: удалите старые.")

class StorageFull(StoreError):
    def __init__(self):
        super().__init__("storage_full", "Хранилище кабинета заполнено: новые данные сейчас не принимаются. "
                                         "Вход, просмотр, выгрузка и удаление работают.")

class QuotaExceeded(StoreError):
    def __init__(self, limit: int):
        super().__init__("quota_exceeded", f"Объём данных в кабинете — не больше {limit // (1024 * 1024)} МБ: "
                                           "удалите старые записи.")

class TooManyTokens(StoreError):
    def __init__(self, limit: int):
        super().__init__("too_many_tokens", f"Токенов API не больше {limit}: удалите ненужные в списке токенов.")

class SignupsPaused(StoreError):
    retry_after = 3600

    def __init__(self):
        super().__init__("signups_paused", "Регистрация временно приостановлена: достигнут суточный предел новых "
                                           "аккаунтов. Повторите позже.")

def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()

def iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def new_id() -> str:
    return uuid.uuid4().hex

class Store:

    def __init__(self, data_dir: str | os.PathLike, data_key: Optional[str] = None,
                 clock: Callable[[], float] = time.time, *, key_file: Optional[str] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES, max_signups_per_day: int = 1000):
        self.data_dir = Path(data_dir)
        self._key_env = data_key
        self._key_file = key_file
        self.clock = clock
        self.max_bytes = int(max_bytes)
        self.max_signups_per_day = int(max_signups_per_day)
        self.user_quota = USER_QUOTA_BYTES
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._fernet = None

    def now(self) -> int:
        return int(self.clock())

    @property
    def db_path(self) -> Path:
        return self.data_dir / DB_NAME

    def _read_key_file(self) -> list[str]:
        path = Path(self._key_file)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                log.error("cabinet: DL_DATA_KEY_FILE is readable by group or others (mode %o); chmod 600", mode)
                raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: файл ключа доступен другим "
                                                        "пользователям (нужны права 0600).")
            text = path.read_text(encoding="ascii")
        except OSError:
            log.error("cabinet: DL_DATA_KEY_FILE cannot be read")
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: не читается файл ключа "
                                                    "DL_DATA_KEY_FILE.") from None
        except UnicodeDecodeError:
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: неверный ключ шифрования.") \
                from None
        try:
            inside = path.resolve().is_relative_to(self.data_dir.resolve())
        except OSError:
            inside = False
        if inside:
            log.warning("cabinet: DL_DATA_KEY_FILE lies in DL_DATA_DIR; keep the key outside the database volume")
        return [k.strip() for k in text.replace("\n", ",").split(",") if k.strip()]

    def _load_key(self):
        from cryptography.fernet import Fernet, MultiFernet

        if self._key_env and self._key_file:
            log.error("cabinet: both DL_DATA_KEY and DL_DATA_KEY_FILE are set")
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: задайте либо DL_DATA_KEY, "
                                                    "либо DL_DATA_KEY_FILE.")
        if self._key_env:
            raw = [k.strip() for k in self._key_env.split(",") if k.strip()]
            source = "DL_DATA_KEY"
        elif self._key_file:
            raw = self._read_key_file()
            source = "DL_DATA_KEY_FILE"
        else:
            path = self.data_dir / KEY_NAME
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                raw = [path.read_text(encoding="ascii").strip()]
                log.warning("cabinet: encryption key is read from file %r; set DL_DATA_KEY outside the demo", str(path))
            else:
                key = Fernet.generate_key()
                with os.fdopen(fd, "wb") as f:
                    f.write(key + b"\n")
                raw = [key.decode("ascii")]
                log.warning("cabinet: DL_DATA_KEY is not set; a new encryption key was written to %r (demo only)",
                            str(path))
            source = "key file"
        try:
            keys = [Fernet(k) for k in raw]
        except (ValueError, TypeError):
            log.error("cabinet: %s is not a valid Fernet key (urlsafe base64 of 32 bytes)", source)
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: неверный ключ шифрования.") from None
        if not keys:
            raise StoreError("storage_unavailable", "Хранилище кабинета недоступно: пустой ключ шифрования.")
        return MultiFernet(keys)

    def _open(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fernet = self._load_key()
                db = self.db_path
                if not db.exists():
                    os.close(os.open(db, os.O_WRONLY | os.O_CREAT, 0o600))
                conn = sqlite3.connect(str(db), check_same_thread=False, isolation_level=None, timeout=15)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(SCHEMA)
                _migrate(conn)
            except StoreError:
                raise
            except (OSError, sqlite3.Error) as e:
                log.error("cabinet: storage is not available (%s)", type(e).__name__)
                raise StoreError("storage_unavailable",
                                 "Хранилище кабинета недоступно: проверьте каталог DL_DATA_DIR.") from None
            self._fernet, self._conn = fernet, conn
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        with self._lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._open()
        with self._lock:
            return conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        conn = self._open()
        with self._lock:
            return conn.execute(sql, params).fetchone()

    def used_bytes(self, c: Optional[sqlite3.Connection] = None) -> int:
        c = c or self._open()
        with self._lock:
            page_size = int(c.execute("PRAGMA page_size").fetchone()[0])
            pages = int(c.execute("PRAGMA page_count").fetchone()[0])
            free = int(c.execute("PRAGMA freelist_count").fetchone()[0])
        return max(0, pages - free) * page_size

    def _check_space(self, c: sqlite3.Connection, extra: int = 0) -> None:
        if self.used_bytes(c) + extra > self.max_bytes:
            log.warning("cabinet: database size limit reached (DL_DATA_MAX_MB)")
            raise StorageFull()

    def checkpoint(self) -> None:
        conn = self._open()
        with self._lock:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error as e:
                log.warning("cabinet: wal checkpoint failed (%s)", type(e).__name__)

    def compact(self, steps: int = VACUUM_MAX_STEPS) -> int:
        conn = self._open()
        done = 0
        for _ in range(int(steps)):
            with self._lock:
                if int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
                    break
                if int(conn.execute("PRAGMA freelist_count").fetchone()[0]) == 0:
                    break
                conn.execute(f"PRAGMA incremental_vacuum({VACUUM_STEP_PAGES})").fetchall()
            done += 1
        return done

    def seal(self, obj: Any, bind: str) -> bytes:
        self._open()
        raw = json.dumps({"b": bind, "d": obj}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(zlib.compress(raw, 6))

    def unseal(self, token: bytes, bind: str) -> Any:
        from cryptography.fernet import InvalidToken

        self._open()
        try:
            data = json.loads(zlib.decompress(self._fernet.decrypt(bytes(token))).decode("utf-8"))
        except (InvalidToken, zlib.error, ValueError):
            log.error("cabinet: a stored field cannot be decrypted (wrong DL_DATA_KEY?)")
            raise StoreError("storage_key", "Данные кабинета не расшифровываются: ключ DL_DATA_KEY не совпадает "
                                            "с ключом, которым они записаны.") from None
        if not isinstance(data, dict) or data.get("b") != bind:
            log.error("cabinet: a stored field is bound to another row")
            raise StoreError("storage_key", "Данные кабинета повреждены.")
        return data.get("d")

    @staticmethod
    def public_user(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "login": row["login"], "role": row["role"], "clinic": row["clinic"],
                "created": iso(row["created"])}

    def create_user(self, login: str, login_key: str, role: str, pw_hash: str, clinic: Optional[str]) -> sqlite3.Row:
        uid, now = new_id(), self.now()
        try:
            with self.tx() as c:
                n = int(c.execute("SELECT COUNT(*) FROM users WHERE created > ?", (now - SIGNUP_WINDOW,)).fetchone()[0])
                if n >= self.max_signups_per_day:
                    log.warning("cabinet: daily signup limit reached (DL_MAX_SIGNUPS_PER_DAY)")
                    raise SignupsPaused()
                self._check_space(c, 4096)
                c.execute("INSERT INTO users (id, login, login_key, role, pw_hash, clinic, consent_at, created) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (uid, login, login_key, role, pw_hash, clinic, now, now))
        except sqlite3.IntegrityError:
            raise LoginTaken() from None
        return self.user_by_id(uid)

    def set_password(self, uid: str, pw_hash: str, keep_session: str) -> int:
        with self.tx() as c:
            c.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (pw_hash, uid))
            return self._revoke_others(c, uid, keep_session)

    def user_by_login(self, login_key: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE login_key = ?", (login_key,))

    def user_by_id(self, uid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT * FROM users WHERE id = ?", (uid,))

    def delete_user(self, uid: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE shares SET doctor_id = NULL, link_sha256 = NULL, revoked = COALESCE(revoked, ?) "
                      "WHERE doctor_id = ? AND patient_id != ?", (self.now(), uid, uid))
            c.execute("DELETE FROM shares WHERE patient_id = ? OR doctor_id = ?", (uid, uid))
            c.execute("DELETE FROM users WHERE id = ?", (uid,))
        self.compact()
        self.checkpoint()

    @staticmethod
    def _insert_session(c: sqlite3.Connection, uid: str, kind: str, expires: int, now: int, *,
                        name: Optional[str] = None, parent_id: Optional[str] = None) -> tuple[str, str]:
        token, sid = secrets.token_urlsafe(32), new_id()
        c.execute("INSERT INTO sessions (id, token_sha256, user_id, kind, name, parent_id, created, expires, "
                  "last_used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                  (sid, sha256_hex(token), uid, kind, name, parent_id, now, int(expires)))
        return token, sid

    def create_login(self, uid: str, ttl: int, api_name: str = "Вход") -> tuple[str, str, sqlite3.Row]:
        now = self.now()
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            cookie, cid = self._insert_session(c, uid, "cookie", now + int(ttl), now)
            api, _ = self._insert_session(c, uid, "api", now + int(ttl), now, name=api_name, parent_id=cid)
            old = [r[0] for r in c.execute("SELECT id FROM sessions WHERE user_id = ? AND kind = 'cookie' "
                                           "ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?", (uid, MAX_LOGINS))]
            for sid in old:
                c.execute("DELETE FROM sessions WHERE (id = ? OR parent_id = ?) AND user_id = ?", (sid, sid, uid))
            c.execute("DELETE FROM sessions WHERE id IN (SELECT id FROM sessions WHERE user_id = ? AND kind = 'api' "
                      "AND parent_id IS NOT NULL ORDER BY created DESC, rowid DESC LIMIT -1 OFFSET ?)",
                      (uid, MAX_LOGINS))
        return cookie, api, self._one("SELECT * FROM sessions WHERE id = ?", (cid,))

    def create_session(self, uid: str, kind: str, ttl: int, *, name: Optional[str] = None,
                       parent_id: Optional[str] = None, expires: Optional[int] = None) -> tuple[str, sqlite3.Row]:
        now = self.now()
        end = now + int(ttl) if expires is None else min(now + int(ttl), int(expires))
        with self.tx() as c:
            c.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            if kind == "api" and parent_id is None:
                n = int(c.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ? AND kind = 'api' AND parent_id "
                                  "IS NULL", (uid,)).fetchone()[0])
                if n >= MAX_NAMED_TOKENS:
                    raise TooManyTokens(MAX_NAMED_TOKENS)
            token_value, sid = self._insert_session(c, uid, kind, end, now, name=name, parent_id=parent_id)
        return token_value, self._one("SELECT * FROM sessions WHERE id = ?", (sid,))

    @staticmethod
    def _revoke_others(c: sqlite3.Connection, uid: str, keep: Optional[str]) -> int:
        keep_ids: set[str] = set()
        if keep:
            row = c.execute("SELECT id, parent_id FROM sessions WHERE id = ? AND user_id = ?", (keep, uid)).fetchone()
            if row is not None:
                keep_ids.add(row["id"])
                if row["parent_id"]:
                    keep_ids.add(row["parent_id"])
                keep_ids.update(r[0] for r in c.execute("SELECT id FROM sessions WHERE parent_id IN (%s) AND "
                                                        "user_id = ?" % ",".join("?" * len(keep_ids)),
                                                        (*keep_ids, uid)))
        if keep_ids:
            marks = ",".join("?" * len(keep_ids))
            return c.execute(f"DELETE FROM sessions WHERE user_id = ? AND id NOT IN ({marks})",
                             (uid, *keep_ids)).rowcount
        return c.execute("DELETE FROM sessions WHERE user_id = ?", (uid,)).rowcount

    def revoke_sessions(self, uid: str, keep: Optional[str]) -> int:
        with self.tx() as c:
            return self._revoke_others(c, uid, keep)

    def session_by_token(self, token: str, kind: str) -> Optional[tuple[sqlite3.Row, sqlite3.Row]]:
        if not token or len(token) > 256:
            return None
        row = self._one("SELECT * FROM sessions WHERE token_sha256 = ? AND kind = ?", (sha256_hex(token), kind))
        if row is None:
            return None
        now = self.now()
        orphan = False
        if row["parent_id"] and row["expires"] > now:
            orphan = self._one("SELECT 1 FROM sessions WHERE id = ? AND expires > ?", (row["parent_id"], now)) is None
        if row["expires"] <= now or orphan:
            with self.tx() as c:
                c.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        user = self.user_by_id(row["user_id"])
        if user is None:
            return None
        if row["last_used"] is None or now - row["last_used"] >= TOUCH_EVERY:
            with self.tx() as c:
                c.execute("UPDATE sessions SET last_used = ? WHERE id = ?", (now, row["id"]))
        return row, user

    def delete_session(self, uid: str, sid: str, kind: Optional[str] = None, *, with_pair: bool = True) -> bool:
        with self.tx() as c:
            row = c.execute("SELECT * FROM sessions WHERE id = ? AND user_id = ?" + (" AND kind = ?" if kind else ""),
                            (sid, uid, kind) if kind else (sid, uid)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM sessions WHERE id = ?", (sid,))
            if with_pair:
                c.execute("DELETE FROM sessions WHERE parent_id = ? AND user_id = ?", (sid, uid))
                if row["parent_id"]:
                    c.execute("DELETE FROM sessions WHERE id = ? AND user_id = ?", (row["parent_id"], uid))
            return True

    def list_tokens(self, uid: str) -> list[sqlite3.Row]:
        now = self.now()
        return self._all("SELECT s.* FROM sessions s WHERE s.user_id = ? AND s.kind = 'api' AND s.expires > ? AND "
                         "(s.parent_id IS NULL OR EXISTS (SELECT 1 FROM sessions p WHERE p.id = s.parent_id AND "
                         "p.expires > ?)) ORDER BY s.created DESC", (uid, now, now))

    def _bind(self, table: str, rid: str, uid: str, part: str = "") -> str:
        return f"{table}{part}:{rid}:{uid}"

    def count_records(self, uid: str) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM records WHERE user_id = ?", (uid,))["n"])

    def user_bytes(self, uid: str, c: Optional[sqlite3.Connection] = None) -> int:
        c = c or self._open()
        with self._lock:
            rec = c.execute("SELECT COALESCE(SUM(LENGTH(enc_payload) + LENGTH(enc_summary)), 0) FROM records "
                            "WHERE user_id = ?", (uid,)).fetchone()[0]
            lab = c.execute("SELECT COALESCE(SUM(LENGTH(enc_ranges)), 0) FROM lab_refs WHERE doctor_id = ?",
                            (uid,)).fetchone()[0]
        return int(rec) + int(lab)

    def _check_quota(self, c: sqlite3.Connection, uid: str, extra: int) -> None:
        if self.user_bytes(uid, c) + extra > self.user_quota:
            raise QuotaExceeded(self.user_quota)
        self._check_space(c, extra)

    def add_records(self, uid: str, items: list[dict], max_records: int) -> list[str]:
        now = self.now()
        ids: list[str] = []
        rows = []
        for it in items:
            rid = new_id()
            payload = self.seal({"input": it["input"], "result": it.get("result")}, self._bind("records", rid, uid))
            summary = self.seal(it["summary"], self._bind("records", rid, uid, ".summary"))
            rows.append((rid, uid, it["date"], it["source"], payload, it["version"], summary, now))
        size = sum(len(r[4]) + len(r[6]) for r in rows)
        with self.tx() as c:
            n = int(c.execute("SELECT COUNT(*) FROM records WHERE user_id = ?", (uid,)).fetchone()[0])
            if n + len(items) > max_records:
                raise TooManyRecords(max_records)
            self._check_quota(c, uid, size)
            for row in rows:
                c.execute("INSERT INTO records (id, user_id, date, source, enc_payload, engine_version, enc_summary, "
                          "created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                ids.append(row[0])
        return ids

    def _record_meta(self, row: sqlite3.Row) -> dict:
        summary = self.unseal(row["enc_summary"], self._bind("records", row["id"], row["user_id"], ".summary")) or {}
        return {"id": row["id"], "date": row["date"], "source": row["source"], "created": iso(row["created"]),
                "summary": summary}

    def list_records(self, uid: str) -> list[dict]:
        rows = self._all("SELECT id, user_id, date, source, enc_summary, created FROM records WHERE user_id = ? "
                         "ORDER BY date DESC, created DESC, rowid DESC", (uid,))
        return [self._record_meta(r) for r in rows]

    def get_record(self, uid: str, rid: str) -> Optional[dict]:
        row = self._one("SELECT * FROM records WHERE id = ? AND user_id = ?", (rid, uid))
        return None if row is None else self._full(row)

    def _full(self, row: sqlite3.Row) -> dict:
        out = self._record_meta(row)
        payload = self.unseal(row["enc_payload"], self._bind("records", row["id"], row["user_id"])) or {}
        out.update(input=payload.get("input"), result=payload.get("result"), version=row["engine_version"])
        return out

    def all_records(self, uid: str) -> list[dict]:
        rows = self._all("SELECT * FROM records WHERE user_id = ? ORDER BY date ASC, created ASC, rowid ASC", (uid,))
        return [self._full(r) for r in rows]

    def update_record_result(self, uid: str, rid: str, inp: dict, result: dict, version: str) -> None:
        payload = self.seal({"input": inp, "result": result}, self._bind("records", rid, uid))
        with self.tx() as c:
            c.execute("UPDATE records SET enc_payload = ?, engine_version = ? WHERE id = ? AND user_id = ?",
                      (payload, version, rid, uid))

    def delete_record(self, uid: str, rid: str) -> bool:
        with self.tx() as c:
            done = c.execute("DELETE FROM records WHERE id = ? AND user_id = ?", (rid, uid)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def last_record(self, uid: str) -> Optional[dict]:
        row = self._one("SELECT id, user_id, date, source, enc_summary, created FROM records WHERE user_id = ? "
                        "ORDER BY date DESC, created DESC, rowid DESC LIMIT 1", (uid,))
        return None if row is None else self._record_meta(row)

    def share_status(self, row: sqlite3.Row) -> str:
        if row["revoked"] is not None:
            return "revoked"
        if row["expires"] <= self.now():
            return "expired"
        return "active" if row["accepted"] is not None else "pending"

    def create_share(self, patient_id: str, days: int) -> tuple[str, sqlite3.Row]:
        token, sid, now = secrets.token_urlsafe(32), new_id(), self.now()
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM shares WHERE patient_id = ? AND revoked IS NULL AND expires > ?",
                          (patient_id, now)).fetchone()[0]
            if n >= MAX_ACTIVE_SHARES:
                raise StoreError("too_many_shares", f"Действующих ссылок и связей не больше {MAX_ACTIVE_SHARES}: "
                                                    "отзовите ненужные.")
            self._check_space(c, 1024)
            c.execute("INSERT INTO shares (id, patient_id, doctor_id, link_sha256, days, created, accepted, expires, "
                      "revoked) VALUES (?, ?, NULL, ?, ?, ?, NULL, ?, NULL)",
                      (sid, patient_id, sha256_hex(token), int(days), now, now + LINK_TTL))
        return token, self._one("SELECT * FROM shares WHERE id = ?", (sid,))

    def list_shares(self, patient_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT s.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM shares s "
                         "LEFT JOIN users u ON u.id = s.doctor_id WHERE s.patient_id = ? "
                         "ORDER BY s.created DESC, s.rowid DESC", (patient_id,))

    def get_share(self, patient_id: str, sid: str) -> Optional[sqlite3.Row]:
        return self._one("SELECT s.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM shares s "
                         "LEFT JOIN users u ON u.id = s.doctor_id WHERE s.patient_id = ? AND s.id = ?",
                         (patient_id, sid))

    def revoke_share(self, patient_id: str, sid: str) -> bool:
        with self.tx() as c:
            done = c.execute("UPDATE shares SET revoked = ?, link_sha256 = NULL WHERE id = ? AND patient_id = ? "
                             "AND revoked IS NULL", (self.now(), sid, patient_id)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def accept_share(self, token: str, doctor_id: str) -> Optional[sqlite3.Row]:
        if not token or len(token) > 256:
            return None
        now = self.now()
        with self.tx() as c:
            row = c.execute("SELECT * FROM shares WHERE link_sha256 = ? AND accepted IS NULL AND revoked IS NULL "
                            "AND expires > ?", (sha256_hex(token), now)).fetchone()
            if row is None:
                return None
            snap = self._doctor_snapshot(c, doctor_id, self._bind("shares", row["id"], row["patient_id"], ".doctor"))
            c.execute("UPDATE shares SET doctor_id = ?, accepted = ?, expires = ?, link_sha256 = NULL, enc_doctor = ? "
                      "WHERE id = ?", (doctor_id, now, now + int(row["days"]) * 86400, snap, row["id"]))
        return self._one("SELECT s.*, u.login AS patient_login FROM shares s JOIN users u ON u.id = s.patient_id "
                         "WHERE s.id = ?", (row["id"],))

    def _doctor_snapshot(self, c: sqlite3.Connection, doctor_id: str, bind: str) -> Optional[bytes]:
        user = c.execute("SELECT login, clinic FROM users WHERE id = ?", (doctor_id,)).fetchone()
        if user is None:
            return None
        return self.seal({"login": user["login"], "clinic": user["clinic"]}, bind)

    def doctor_from_snapshot(self, row: sqlite3.Row, table: str) -> Optional[dict]:
        if row["doctor_id"] is not None:
            return {"login": row["doctor_login"], "clinic": row["doctor_clinic"]}
        if row["enc_doctor"] is None:
            return None
        snap = self.unseal(row["enc_doctor"], self._bind(table, row["id"], row["patient_id"], ".doctor")) or {}
        if not snap.get("login"):
            return None
        return {"login": snap.get("login"), "clinic": snap.get("clinic"), "deleted": True}

    def has_access(self, doctor_id: str, patient_id: str) -> bool:
        row = self._one("SELECT 1 FROM shares WHERE doctor_id = ? AND patient_id = ? AND accepted IS NOT NULL "
                        "AND revoked IS NULL AND expires > ? LIMIT 1", (doctor_id, patient_id, self.now()))
        return row is not None

    def doctor_patients(self, doctor_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT u.id AS id, u.login AS login, MAX(s.expires) AS expires, "
                         "(SELECT COUNT(*) FROM records r WHERE r.user_id = u.id) AS n_records "
                         "FROM shares s JOIN users u ON u.id = s.patient_id "
                         "WHERE s.doctor_id = ? AND s.accepted IS NOT NULL AND s.revoked IS NULL AND s.expires > ? "
                         "GROUP BY u.id ORDER BY MAX(s.accepted) DESC", (doctor_id, self.now()))

    def log_access(self, patient_id: str, doctor_id: str, what: str, record_id: Optional[str] = None) -> None:
        now = self.now()
        with self.tx() as c:
            row = c.execute("SELECT id, record_id, enc_doctor FROM access_log WHERE patient_id = ? AND doctor_id = ? "
                            "AND what = ? AND at > ? ORDER BY at DESC, id DESC LIMIT 1",
                            (patient_id, doctor_id, what, now - ACCESS_LOG_MERGE)).fetchone()
            if row is None:
                lid = c.execute("INSERT INTO access_log (patient_id, doctor_id, what, record_id, at) "
                                "VALUES (?, ?, ?, ?, ?)", (patient_id, doctor_id, what, record_id, now)).lastrowid
            else:
                lid = row["id"]
                if row["record_id"] is not None and row["record_id"] != record_id:
                    c.execute("UPDATE access_log SET record_id = NULL WHERE id = ?", (lid,))
            if row is None or row["enc_doctor"] is None:
                snap = self._doctor_snapshot(c, doctor_id, self._bind("access_log", str(lid), patient_id, ".doctor"))
                c.execute("UPDATE access_log SET enc_doctor = ? WHERE id = ?", (snap, lid))

    def access_log(self, patient_id: str) -> list[sqlite3.Row]:
        return self._all("SELECT a.*, u.login AS doctor_login, u.clinic AS doctor_clinic FROM access_log a "
                         "LEFT JOIN users u ON u.id = a.doctor_id WHERE a.patient_id = ? "
                         "ORDER BY a.at DESC, a.id DESC LIMIT ?", (patient_id, ACCESS_LOG_LIMIT))

    def _lab_set(self, row: sqlite3.Row) -> dict:
        ranges = self.unseal(row["enc_ranges"], self._bind("lab_refs", row["id"], row["doctor_id"])) or {}
        return {"id": row["id"], "name": row["name"], "default": bool(row["is_default"]), "ranges": ranges}

    def list_lab_refs(self, doctor_id: str) -> list[dict]:
        rows = self._all("SELECT * FROM lab_refs WHERE doctor_id = ? ORDER BY created ASC, rowid ASC", (doctor_id,))
        return [self._lab_set(r) for r in rows]

    def add_lab_ref(self, doctor_id: str, name: str, ranges: dict, default: bool) -> str:
        lid = new_id()
        enc = self.seal(ranges, self._bind("lab_refs", lid, doctor_id))
        with self.tx() as c:
            n = c.execute("SELECT COUNT(*) FROM lab_refs WHERE doctor_id = ?", (doctor_id,)).fetchone()[0]
            if n >= MAX_LAB_SETS:
                raise StoreError("too_many_sets", f"Наборов референсов не больше {MAX_LAB_SETS}: удалите ненужные.")
            self._check_quota(c, doctor_id, len(enc))
            if default:
                c.execute("UPDATE lab_refs SET is_default = 0 WHERE doctor_id = ?", (doctor_id,))
            c.execute("INSERT INTO lab_refs (id, doctor_id, name, enc_ranges, is_default, created) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (lid, doctor_id, name, enc, 1 if default else 0, self.now()))
        return lid

    def update_lab_ref(self, doctor_id: str, lid: str, *, name: Optional[str] = None, ranges: Optional[dict] = None,
                       default: Optional[bool] = None) -> bool:
        enc = self.seal(ranges, self._bind("lab_refs", lid, doctor_id)) if ranges is not None else None
        with self.tx() as c:
            row = c.execute("SELECT LENGTH(enc_ranges) FROM lab_refs WHERE id = ? AND doctor_id = ?",
                            (lid, doctor_id)).fetchone()
            if row is None:
                return False
            if enc is not None:
                self._check_quota(c, doctor_id, max(0, len(enc) - int(row[0])))
                c.execute("UPDATE lab_refs SET enc_ranges = ? WHERE id = ?", (enc, lid))
            if name is not None:
                c.execute("UPDATE lab_refs SET name = ? WHERE id = ?", (name, lid))
            if default is True:
                c.execute("UPDATE lab_refs SET is_default = (id = ?) WHERE doctor_id = ?", (lid, doctor_id))
            elif default is False:
                c.execute("UPDATE lab_refs SET is_default = 0 WHERE id = ?", (lid,))
        if enc is not None:
            self.checkpoint()
        return True

    def delete_lab_ref(self, doctor_id: str, lid: str) -> bool:
        with self.tx() as c:
            done = c.execute("DELETE FROM lab_refs WHERE id = ? AND doctor_id = ?", (lid, doctor_id)).rowcount > 0
        if done:
            self.checkpoint()
        return done

    def default_lab_ref(self, doctor_id: str) -> Optional[dict]:
        row = self._one("SELECT * FROM lab_refs WHERE doctor_id = ? AND is_default = 1 LIMIT 1", (doctor_id,))
        return None if row is None else self._lab_set(row)
