#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
9Router Codex OAuth Importer (macOS)
====================================
Tool ho tro nap tai khoan Codex OAuth vao 9Router tren macOS.
Khong yeu cau cai them thu vien (chi dung Python Standard Library).
Tool uu tien Bulk Import API va chi fallback vao SQLite khi API khong san sang.

Cach dung:
  1. Mo Terminal trong thu muc da giai nen va chay:
     python3 import_to_9router_macos.py

  2. Chi dinh file cu the:
     python3 import_to_9router_macos.py --file tokens.json

  3. Chi dinh URL 9Router tuy chinh (neu doi port hoac chay tu xa):
     python3 import_to_9router_macos.py --url http://192.168.1.100:20128/api/oauth/codex/bulk-import
"""

import os
import sys
import json
import uuid
import sqlite3
import time
import glob
import argparse
import urllib.request
import urllib.error
import traceback

# ======================================================================
# SAFE PRINT: Dam bao print khong bao gio crash tren moi terminal
# ======================================================================
_original_print = print

def safe_print(*args, **kwargs):
    """Print wrapper an toan, fallback khi terminal khong ho tro Unicode."""
    try:
        _original_print(*args, **kwargs)
    except UnicodeEncodeError:
        # Fallback: encode lai toan bo text thanh ASCII an toan
        text = " ".join(str(a) for a in args)
        safe_text = text.encode("ascii", errors="replace").decode("ascii")
        _original_print(safe_text, **{k: v for k, v in kwargs.items() if k != "end"})
    except Exception:
        pass

print = safe_print

# Thiet lap encoding an toan cho terminal (Python 3.7+)
try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def get_app_dir():
    """
    Lay thu muc chua file .exe hoac .py hien tai.
    Tuong thich ca khi chay script Python va khi dong goi PyInstaller .exe.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller .exe => thu muc chua file .exe
        return os.path.dirname(os.path.abspath(sys.executable))
    else:
        # Chay bang python => thu muc chua file .py
        return os.path.dirname(os.path.abspath(__file__))


def print_banner():
    print("")
    print("=" * 65)
    print("          9ROUTER CODEX OAUTH IMPORTER (macOS)")
    print("       Ho tro nap Token OAuth vao 9Router tren macOS")
    print("=" * 65)
    print("")


def wait_for_user_exit():
    """Giu Terminal mo de nguoi dung doc thong bao loi / ket qua."""
    try:
        print("")
        print("-" * 65)
        print("  [HOAN TAT] Qua trinh xu ly da ket thuc.")
        print("  >> Nhan phim ENTER de dong cua so nay...")
        print("-" * 65)
        input()
    except Exception:
        # Neu input() bi loi (vi du piped stdin), sleep 30s de user kip doc
        try:
            time.sleep(30)
        except Exception:
            pass


def find_9router_db_paths():
    """Tim database 9Router trong cac vi tri macOS pho bien."""
    home = os.path.expanduser("~")
    candidates = []

    # 1. Bien moi truong DATA_DIR neu nguoi dung da doi vi tri luu tru.
    env_data_dir = os.environ.get("DATA_DIR", "").strip()
    if env_data_dir:
        candidates.append(os.path.join(env_data_dir, "db", "data.sqlite"))
        candidates.append(os.path.join(env_data_dir, "data.sqlite"))

    # 2. Vi tri macOS cua Electron (thuong phan biet hoa thuong theo volume).
    candidates.extend([
        os.path.join(home, "Library", "Application Support", "9Router", "db", "data.sqlite"),
        os.path.join(home, "Library", "Application Support", "9Router", "data.sqlite"),
        os.path.join(home, "Library", "Application Support", "9router", "db", "data.sqlite"),
        os.path.join(home, "Library", "Application Support", "9router", "data.sqlite"),
        os.path.join(home, "Library", "Application Support", "com.9router.app", "db", "data.sqlite"),
        os.path.join(home, "Library", "Application Support", "com.9router.app", "data.sqlite"),
        os.path.join(home, ".9router", "db", "data.sqlite"),
        os.path.join(home, ".9router", "data.sqlite"),
    ])

    existing = []
    checked = []
    for p in candidates:
        try:
            abs_p = os.path.abspath(p)
        except Exception:
            continue
        if abs_p not in checked:
            checked.append(abs_p)
            try:
                if os.path.exists(abs_p):
                    existing.append(abs_p)
            except Exception:
                pass

    return existing, checked


def parse_accounts_from_file(file_path):
    """
    Doc tai khoan tu moi dinh dang pho bien:
    - JSON Array: [ { "accessToken": ... }, ... ]
    - JSON Object don le: { "accessToken": ... }
    - JSONL: moi dong 1 JSON object
    - Text hon hop: email|password|2fa|{"accessToken": ...}
    - Danh sach JSON thieu ngoac vuong: { "accessToken": ... }, { "accessToken": ... }
    """
    if not file_path:
        print("[LOI] Duong dan file trong, chua chon file.")
        return [], 0, "Duong dan file trong"

    # Chuan hoa duong dan
    file_path = os.path.abspath(file_path)

    if not os.path.exists(file_path):
        print("[LOI FILE] Khong tim thay file tai duong dan:")
        print("   -> '%s'" % file_path)
        return [], 0, "Khong tim thay file"

    try:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            print("[LOI FILE] File rong (0 bytes), khong co du lieu de import.")
            return [], 0, "File rong (0 bytes)"
        print("   Kich thuoc file: %.1f KB" % (file_size / 1024))
    except Exception as e:
        print("[CANH BAO] Khong kiem tra duoc kich thuoc file: %s" % e)

    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_text = f.read().strip()
    except PermissionError:
        print("[LOI DOC FILE] Khong co quyen doc file '%s'." % file_path)
        print("   -> Thu chay lai voi quyen Admin / root.")
        return [], 0, "Khong co quyen doc file"
    except Exception as e:
        print("[LOI DOC FILE] Khong the doc noi dung file:")
        print("   -> Chi tiet: %s" % e)
        return [], 0, "Loi doc file: %s" % e

    if not raw_text:
        print("[LOI FILE] File khong co noi dung chu hoac toan khoang trang.")
        return [], 0, "Noi dung file rong"

    accounts = []
    total_lines = len(raw_text.splitlines())

    # 1. Thu parse toan bo file duoi dang JSON Array hoac JSON Object
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("accessToken"):
                    accounts.append(item)
            if accounts:
                return accounts, total_lines, "JSON Array"
        elif isinstance(parsed, dict):
            if parsed.get("accessToken"):
                return [parsed], total_lines, "JSON Object"
            elif isinstance(parsed.get("accounts"), list):
                for item in parsed["accounts"]:
                    if isinstance(item, dict) and item.get("accessToken"):
                        accounts.append(item)
                if accounts:
                    return accounts, total_lines, "JSON Object (accounts key)"
    except Exception:
        # 2. Thu boc ngoac vuong neu nguoi dung copy dang { "accessToken": ... }, { "accessToken": ... }
        try:
            wrapped = json.loads("[%s]" % raw_text.strip().rstrip(","))
            if isinstance(wrapped, list):
                for item in wrapped:
                    if isinstance(item, dict) and item.get("accessToken"):
                        accounts.append(item)
                if accounts:
                    return accounts, total_lines, "Comma-separated JSON Objects"
        except Exception:
            pass

    # 3. Neu khong phai JSON nguyen khoi, duyet tung dong (JSONL hoac email|pass|2fa|{JSON})
    lines = raw_text.splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # Thu parse dong thanh JSON truc tiep
        try:
            item = json.loads(line)
            if isinstance(item, dict) and item.get("accessToken"):
                accounts.append(item)
                continue
        except Exception:
            pass

        # Thu tim phan JSON trong dinh dang email|pass|2fa|{...}
        json_start = line.find("{")
        json_end = line.rfind("}")
        if json_start != -1 and json_end != -1 and json_end > json_start:
            json_substr = line[json_start : json_end + 1]
            try:
                item = json.loads(json_substr)
                if isinstance(item, dict) and item.get("accessToken"):
                    # Neu thieu email thi lay tu phan text phia truoc
                    prefix = line[:json_start].strip().rstrip("|")
                    if prefix and not item.get("email"):
                        parts = prefix.split("|")
                        if parts:
                            item["email"] = parts[0].strip()
                    accounts.append(item)
                    continue
            except Exception:
                pass

    if accounts:
        return accounts, total_lines, "Dong ket hop (JSONL / email|pass|2fa|{JSON})"

    return [], total_lines, "Khong tim thay token hop le"


def deduplicate_accounts(accounts):
    """Loc trung tai khoan dua tren email hoac accessToken."""
    unique = []
    seen = set()
    for item in accounts:
        email = str(item.get("email") or "").strip().lower()
        token = str(item.get("accessToken") or "").strip()
        key = email or token
        if key and key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def import_via_api(accounts, api_url):
    """Nap danh sach tai khoan qua REST API cua 9Router (chuan HTTP POST)."""
    try:
        data_bytes = json.dumps(accounts, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=data_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "9Router-macOS-Importer/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            if 200 <= response.status < 300:
                resp_text = response.read().decode("utf-8", errors="ignore")
                try:
                    res = json.loads(resp_text)
                    success_count = res.get("success", len(accounts))
                    failed_count = res.get("failed", 0)
                except Exception:
                    success_count = len(accounts)
                    failed_count = 0

                print("[API 9ROUTER] Nap thanh cong qua API!")
                print("   -> Da nap thanh cong: %d tai khoan" % success_count)
                if failed_count > 0:
                    print("   -> Bi tu choi/that bai: %d tai khoan" % failed_count)
                return True, "Thanh cong qua API"
            else:
                return False, "API tra ve ma loi HTTP %d" % response.status
    except urllib.error.HTTPError as e:
        err_msg = "HTTP %d: %s" % (e.code, e.reason)
        try:
            body = e.read().decode("utf-8", errors="ignore")
            if body:
                err_msg += " (%s)" % body[:150]
        except Exception:
            pass
        return False, err_msg
    except urllib.error.URLError as e:
        return False, "Khong the ket noi den %s (Ly do: %s)" % (api_url, e.reason)
    except Exception as e:
        return False, "Loi goi API: %s" % e


def import_via_sqlite(accounts, custom_db_path=None):
    """Ghi truc tiep danh sach tai khoan vao Database SQLite cua 9Router."""
    db_path = custom_db_path
    if not db_path:
        existing_paths, all_paths = find_9router_db_paths()
        if existing_paths:
            db_path = existing_paths[0]
        else:
            print("")
            print("[LOI DATABASE] Khong tim thay co so du lieu SQLite cua 9Router tren may nay.")
            print("   Da kiem tra qua cac thu muc sau nhung khong thay data.sqlite:")
            for p in all_paths[:6]:
                print("   - %s" % p)
            print("")
            print("HUONG DAN KHAC PHUC:")
            print("   1. Hay mo 9Router len 1 lan de tao du lieu tren macOS.")
            print("   2. Bat 9Router va chay lai tool de nap qua API cong 20128.")
            print("   3. Hoac mo Web 9Router -> Codex -> Bulk Import -> dan noi dung JSON vao.")
            print("   4. Neu database o vi tri khac, chay: python3 import_to_9router_macos.py --db \"duong_dan_data.sqlite\"")
            return False, "Khong tim thay file SQLite 9Router"

    print("")
    print("[SQLITE] Tim thay database 9Router tai:")
    print("   -> %s" % db_path)
    try:
        conn = sqlite3.connect(db_path, timeout=15)
        cur = conn.cursor()

        # Kiem tra bang providerConnections
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='providerConnections'")
        if not cur.fetchone():
            print("[LOI DATABASE] Bang 'providerConnections' chua duoc khoi tao trong database nay.")
            print("   -> Hay khoi dong phan mem 9Router len 1 lan de he thong tu tao cau truc bang.")
            conn.close()
            return False, "Bang providerConnections chua ton tai"

        # Lay priority cao nhat hien tai
        cur.execute("SELECT COALESCE(MAX(priority), 0) FROM providerConnections WHERE provider = 'codex'")
        max_p = cur.fetchone()[0] or 0

        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        imported_count = 0
        updated_count = 0
        failed_count = 0

        for item in accounts:
            try:
                email = str(item.get("email") or "").strip()
                access_token = str(item.get("accessToken") or "").strip()
                if not access_token:
                    failed_count += 1
                    continue

                name = item.get("name") or email or "OpenAI Codex"
                refresh_token = item.get("refreshToken", "")
                id_token = item.get("idToken", "")
                expires_in = int(item.get("expiresIn", 864000) or 864000)
                expires_at = item.get("expiresAt") or time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in)
                )
                psd = item.get("providerSpecificData") or {}

                extra_data = {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "idToken": id_token,
                    "expiresIn": expires_in,
                    "expiresAt": expires_at,
                    "providerSpecificData": psd,
                    "testStatus": item.get("testStatus", "active"),
                    "lastRefreshAt": item.get("lastRefreshAt", now_iso),
                }
                data_json = json.dumps(extra_data, ensure_ascii=False)

                # Kiem tra xem account da ton tai trong DB chua
                cur.execute(
                    "SELECT id FROM providerConnections WHERE provider = 'codex' AND (email = ? OR (name = ? AND name != ''))",
                    (email, name),
                )
                row = cur.fetchone()

                if row:
                    conn_id = row[0]
                    cur.execute(
                        """
                        UPDATE providerConnections
                        SET name = ?, email = ?, isActive = 1, data = ?, updatedAt = ?
                        WHERE id = ?
                        """,
                        (name, email, data_json, now_iso, conn_id),
                    )
                    updated_count += 1
                else:
                    max_p += 1
                    conn_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
                        VALUES (?, 'codex', 'oauth', ?, ?, ?, 1, ?, ?, ?)
                        """,
                        (conn_id, name, email, max_p, data_json, now_iso, now_iso),
                    )
                    imported_count += 1
            except Exception as item_err:
                print("[CANH BAO] Khong ghi duoc nick '%s': %s" % (item.get("email", "unknown"), item_err))
                failed_count += 1

        conn.commit()
        conn.close()

        print("")
        print("[THANH CONG] Da ghi du lieu vao SQLite 9Router hoan tat!")
        print("   -> Them moi: %d tai khoan" % imported_count)
        print("   -> Cap nhat lai: %d tai khoan" % updated_count)
        if failed_count > 0:
            print("   -> Loi bo qua: %d tai khoan" % failed_count)
        return True, "Thanh cong qua SQLite"
    except sqlite3.OperationalError as e:
        print("[LOI SQLITE] Loi thao tac co so du lieu (co the file dang bi khoa boi tien trinh khac): %s" % e)
        return False, "Loi SQLite: %s" % e
    except Exception as e:
        print("[LOI SQLITE] Loi khong xac dinh khi ghi SQLite: %s" % e)
        return False, "Loi SQLite: %s" % e


def select_file_interactive():
    """Menu tuong tac cho nguoi dung chon file hoac keo tha file vao cua so terminal."""
    app_dir = get_app_dir()
    current_dir = os.getcwd()
    search_dirs = [current_dir]
    if os.path.abspath(app_dir) != os.path.abspath(current_dir):
        search_dirs.append(app_dir)

    # Tim cac file tiem nang trong cung thu muc
    candidates = []
    for d in search_dirs:
        try:
            for ext in ("*.txt", "*.json", "*.jsonl"):
                for f in glob.glob(os.path.join(d, ext)):
                    f_abs = os.path.abspath(f)
                    bname = os.path.basename(f_abs).lower()
                    # Loai tru cac file he thong
                    if bname in ("failed.txt", "die.txt", "proxy.txt", "import_to_9router.spec"):
                        continue
                    if f_abs not in candidates and os.path.isfile(f_abs):
                        candidates.append(f_abs)
        except Exception:
            pass

    if candidates:
        print("Danh sach file token tim thay trong cung thu muc:")
        for idx, path in enumerate(candidates, 1):
            try:
                size_kb = os.path.getsize(path) / 1024
            except Exception:
                size_kb = 0
            rel_name = os.path.basename(path)
            print("   [%d] %s (%.1f KB)" % (idx, rel_name, size_kb))
        print("   [0] Nhap duong dan file khac hoac keo tha file truc tiep vao day")
        print("-" * 65)

        try:
            choice = input(">> Chon so file can nap [1-%d] (Mac dinh: 1): " % len(candidates)).strip()
        except EOFError:
            choice = "1"

        if not choice:
            return candidates[0]

        if choice.isdigit():
            num = int(choice)
            if 1 <= num <= len(candidates):
                return candidates[num - 1]
            elif num == 0:
                pass  # fall through to manual input below
            else:
                print("[CANH BAO] Lua chon '%s' khong hop le." % choice)
                return None

    # Nhap tay hoac keo tha
    print("")
    try:
        custom_input = input(">> Keo tha file hoac dan duong dan file token vao day: ").strip()
    except EOFError:
        custom_input = ""

    # Xoa dau nhay kep / nhay don neu keo tha file tu Finder vao Terminal.
    custom_input = custom_input.strip("\"' ")
    return custom_input if custom_input else None


def run_app():
    print_banner()

    parser = argparse.ArgumentParser(
        description="Nap danh sach tai khoan Codex OAuth vao 9Router tren macOS"
    )
    parser.add_argument(
        "--file", "-f",
        default=None,
        help="Duong dan toi file JSON, JSONL hoac text chua token",
    )
    parser.add_argument(
        "--url", "-u",
        default="http://127.0.0.1:20128/api/oauth/codex/bulk-import",
        help="URL Bulk-Import API cua 9Router (mac dinh: http://127.0.0.1:20128/api/oauth/codex/bulk-import)",
    )
    parser.add_argument(
        "--db", "-d",
        default=None,
        help="Duong dan truc tiep toi file data.sqlite cua 9Router (tuy chon)",
    )
    args = parser.parse_args()

    file_path = args.file
    if not file_path:
        file_path = select_file_interactive()

    if not file_path:
        print("")
        print("[LOI] Ban chua chon file hoac duong dan file khong hop le.")
        return

    print("")
    print("Dang phan tich file: '%s' ..." % file_path)
    raw_accounts, total_lines, format_desc = parse_accounts_from_file(file_path)

    if not raw_accounts:
        print("")
        print("[KET QUA] Khong tim thay tai khoan OAuth nao hop le trong file!")
        print("   -> Dinh dang nhan dien: %s" % format_desc)
        print("   -> Tong so dong trong file: %d" % total_lines)
        print("")
        print("HUONG DAN:")
        print("   Hay dam bao file chua thong tin tai khoan hoac chuoi Token OAuth (co truong 'accessToken').")
        return

    accounts = deduplicate_accounts(raw_accounts)
    print("[OK] Da nhan dien: %d tai khoan (Dinh dang: %s)" % (len(raw_accounts), format_desc))
    if len(accounts) < len(raw_accounts):
        print("   -> Da loc trung: con lai %d tai khoan duy nhat." % len(accounts))

    # Buoc 1: Thu nap qua API 9Router (neu 9Router dang chay)
    print("")
    print("[BUOC 1] Thu nap qua 9Router REST API (%s)..." % args.url)
    api_ok, api_msg = import_via_api(accounts, args.url)
    if api_ok:
        print("")
        print("[HOAN TAT] Tat ca tai khoan da duoc dong bo vao 9Router thanh cong qua API!")
        return

    print("[THONG BAO] API khong phan hoi: %s" % api_msg)
    print("   -> 9Router chua duoc bat hoac chay port khac. Tu dong chuyen sang BUOC 2...")

    # Buoc 2: Neu 9Router tat, tu dong nap thang vao SQLite Database
    print("")
    print("[BUOC 2] Nap truc tiep vao Database SQLite cua 9Router...")
    db_ok, db_msg = import_via_sqlite(accounts, args.db)
    if db_ok:
        print("")
        print("[HOAN TAT] Tat ca tai khoan da duoc nap truc tiep vao Database cua 9Router!")
    else:
        print("")
        print("[THAT BAI] Khong the nap duoc vao 9Router qua ca 2 phuong thuc (API & SQLite).")
        print("   -> Chi tiet loi: %s" % db_msg)


def main():
    try:
        run_app()
    except KeyboardInterrupt:
        print("")
        print("[HUY] Da huy thao tac boi nguoi dung (Ctrl+C).")
    except Exception as e:
        print("")
        print("[LOI HE THONG] Loi khong xac dinh: %s" % e)
        try:
            traceback.print_exc()
        except Exception:
            pass
    finally:
        wait_for_user_exit()


if __name__ == "__main__":
    main()
