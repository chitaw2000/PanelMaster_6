import os
import threading
import time
import requests


_scheduler_started = False
_scheduler_lock = threading.Lock()


def send_backup_to_telegram(bot_token, admin_id, file_path, caption=""):
    token = str(bot_token or "").strip()
    chat_id = str(admin_id or "").strip()
    if not token or not chat_id:
        return False, "backup bot token/admin id is empty"
    if not file_path or not os.path.exists(file_path):
        return False, "backup file not found"

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    try:
        with open(file_path, "rb") as fp:
            files = {"document": fp}
            data = {"chat_id": chat_id, "caption": caption[:1024]}
            res = requests.post(url, data=data, files=files, timeout=45)
        if 200 <= res.status_code < 300:
            return True, "ok"
        return False, f"http {res.status_code}: {res.text[:200]}"
    except Exception as e:
        return False, str(e)


def start_backup_scheduler(load_config_fn, save_config_fn, create_backup_file_fn, log_fn=None, poll_seconds=60):
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True

    def _log(msg, level="info"):
        if log_fn:
            try:
                log_fn("Backup Bot", msg, level)
            except Exception:
                pass

    def _worker():
        while True:
            try:
                cfg = load_config_fn() or {}
                enabled = bool(cfg.get("backup_bot_enabled", False))
                token = str(cfg.get("backup_bot_token", "")).strip()
                admin_id = str(cfg.get("backup_bot_admin_id", "")).strip()
                try:
                    hours = float(cfg.get("backup_bot_interval_hours", 1) or 1)
                except Exception:
                    hours = 1.0
                hours = max(1.0, hours)
                try:
                    last_sent = float(cfg.get("backup_bot_last_sent_ts", 0) or 0)
                except Exception:
                    last_sent = 0.0

                now = time.time()
                due = enabled and token and admin_id and (now - last_sent >= hours * 3600)
                if due:
                    backup_ref, backup_path = create_backup_file_fn("auto_telegram")
                    ok, msg = send_backup_to_telegram(
                        token,
                        admin_id,
                        backup_path,
                        caption=f"PanelMaster Auto Backup\nFile: {backup_ref}"
                    )
                    if ok:
                        cfg["backup_bot_last_sent_ts"] = now
                        save_config_fn(cfg)
                        _log(f"Auto backup sent: {backup_ref}", "success")
                    else:
                        _log(f"Auto backup send failed: {msg}", "error")
            except Exception as e:
                _log(f"Scheduler error: {e}", "error")

            time.sleep(max(20, int(poll_seconds)))

    threading.Thread(target=_worker, daemon=True).start()
