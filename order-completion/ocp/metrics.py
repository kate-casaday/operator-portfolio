"""Instrumentation helpers and the metrics roll-up used by the dashboard, tests and
the cost calculator.  Every number is read back from rows the engine wrote; labels say
what each number is (provider-accepted is not delivered; logged minutes are not assumed minutes)."""
from __future__ import annotations

import math
import sqlite3
from typing import Dict

from .db import rows, row

GSM7_BASIC = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€")


def sms_segments(body: str) -> int:
    """GSM-7: 160 chars single, 153 per segment when concatenated (ext chars count 2).
    UCS-2 fallback counts UTF-16 code units (emoji = 2): 70 single, 67 per segment."""
    if not body:
        return 1
    if all(c in GSM7_BASIC or c in GSM7_EXT for c in body):
        length = sum(2 if c in GSM7_EXT else 1 for c in body)
        return 1 if length <= 160 else math.ceil(length / 153.0)
    units = len(body.encode("utf-16-le")) // 2
    return 1 if units <= 70 else math.ceil(units / 67.0)


def summary(conn: sqlite3.Connection) -> Dict:
    orders_by_state = {r["state"]: r["n"] for r in rows(conn, "SELECT state, COUNT(*) n FROM orders GROUP BY state")}
    conv_by_state = {r["state"]: r["n"] for r in rows(conn, "SELECT state, COUNT(*) n FROM conversations GROUP BY state")}
    mc = row(conn, "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o, "
                   "COALESCE(SUM(cache_read_tokens),0) c, COALESCE(AVG(latency_ms),0) lat, "
                   "SUM(CASE WHEN simulated=1 THEN 1 ELSE 0 END) sim, "
                   "SUM(CASE WHEN outcome='ok' THEN 1 ELSE 0 END) ok_n, "
                   "SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) err_n, "
                   "SUM(CASE WHEN outcome='fallback' THEN 1 ELSE 0 END) fb_n, "
                   "SUM(CASE WHEN outcome='discarded_low_confidence' THEN 1 ELSE 0 END) disc_n FROM model_calls") or {}
    sms = row(conn, "SELECT "
                    "SUM(CASE WHEN direction='outbound' AND status='sent' THEN segments ELSE 0 END) out_seg, "
                    "SUM(CASE WHEN direction='outbound' AND status='sent' THEN 1 ELSE 0 END) out_msg, "
                    "SUM(CASE WHEN direction='inbound' THEN segments ELSE 0 END) in_seg, "
                    "SUM(CASE WHEN direction='inbound' THEN 1 ELSE 0 END) in_msg, "
                    "SUM(CASE WHEN direction='outbound' AND status='queued' THEN 1 ELSE 0 END) queued, "
                    "SUM(CASE WHEN direction='outbound' AND status='failed' THEN 1 ELSE 0 END) failed, "
                    "SUM(CASE WHEN direction='outbound' AND status='ambiguous' THEN 1 ELSE 0 END) ambiguous, "
                    "SUM(CASE WHEN direction='outbound' AND status IN ('suppressed','cancelled') THEN 1 ELSE 0 END) withheld "
                    "FROM messages") or {}
    esc = row(conn, "SELECT COUNT(*) n, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) open_n, "
                    "SUM(CASE WHEN queue='clinician' THEN 1 ELSE 0 END) clin_n, "
                    "SUM(CASE WHEN queue='clinician' AND status='open' THEN 1 ELSE 0 END) clin_open, "
                    "SUM(CASE WHEN queue='clinician' AND overdue=1 AND status='open' THEN 1 ELSE 0 END) clin_overdue, "
                    "SUM(CASE WHEN queue='kate' AND status='open' THEN 1 ELSE 0 END) kate_open "
                    "FROM escalations") or {}
    ht = row(conn, "SELECT COALESCE(SUM(CASE WHEN source='logged' THEN minutes ELSE 0 END),0) logged, "
                   "COALESCE(SUM(CASE WHEN source='default_assumed' THEN minutes ELSE 0 END),0) assumed FROM human_time") or {}
    clin_minutes = row(conn, "SELECT COALESCE(SUM(human_minutes),0) m FROM escalations WHERE queue='clinician' AND status='resolved'") or {}
    verified = row(conn, "SELECT COUNT(*) n FROM orders WHERE state='verified_complete'") or {}
    external = row(conn, "SELECT COUNT(*) n FROM orders WHERE state='completed_external'") or {}
    prefs = row(conn, "SELECT COUNT(*) n FROM preferences WHERE superseded_by IS NULL") or {}
    fbk = row(conn, "SELECT COUNT(*) n, SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) o FROM feedback") or {}
    claimed = row(conn, "SELECT COUNT(*) n FROM orders WHERE state='claimed_complete'") or {}
    patients = row(conn, "SELECT COUNT(*) n FROM patients") or {}
    convs = row(conn, "SELECT COUNT(*) n FROM conversations") or {}
    no_esc = row(conn, "SELECT COUNT(*) n FROM conversations c WHERE NOT EXISTS "
                       "(SELECT 1 FROM escalations e WHERE e.conversation_id=c.id)") or {}
    verified_no_esc = row(conn, "SELECT COUNT(DISTINCT c.id) n FROM conversations c JOIN orders o ON o.patient_id=c.patient_id "
                                "WHERE o.state='verified_complete' AND NOT EXISTS "
                                "(SELECT 1 FROM escalations e WHERE e.conversation_id=c.id)") or {}
    return {
        "patients": patients.get("n", 0),
        "conversations": convs.get("n", 0),
        "orders_by_state": orders_by_state,
        "conversations_by_state": conv_by_state,
        "verified_completions": verified.get("n", 0),
        "completed_external": external.get("n", 0),
        "claimed_not_verified": claimed.get("n", 0),
        "active_preferences": prefs.get("n", 0),
        "feedback": {"total": fbk.get("n", 0) or 0, "open": fbk.get("o", 0) or 0},
        "model": {
            "attempts_recorded": mc.get("n", 0) or 0,
            "ok": mc.get("ok_n", 0) or 0, "errors": mc.get("err_n", 0) or 0,
            "fallbacks": mc.get("fb_n", 0) or 0, "discarded_low_confidence": mc.get("disc_n", 0) or 0,
            "input_tokens": mc.get("i", 0) or 0,
            "output_tokens": mc.get("o", 0) or 0,
            "cache_read_tokens": mc.get("c", 0) or 0,
            "avg_latency_ms": round(mc.get("lat", 0) or 0, 1),
            "simulated_attempts": mc.get("sim", 0) or 0,
            "note": "token counts from the mock adapter are chars/4 estimates; failed attempts carry no usage",
        },
        "sms": {
            "provider_accepted_messages": sms.get("out_msg", 0) or 0,
            "provider_accepted_segments": sms.get("out_seg", 0) or 0,
            "inbound_messages": sms.get("in_msg", 0) or 0,
            "inbound_segments_original_length": sms.get("in_seg", 0) or 0,
            "queued": sms.get("queued", 0) or 0,
            "failed": sms.get("failed", 0) or 0,
            "ambiguous_after_crash": sms.get("ambiguous", 0) or 0,
            "withheld_suppressed_or_cancelled": sms.get("withheld", 0) or 0,
            "note": "'provider_accepted' is not 'delivered'; delivery receipts are not modeled in v0.2",
        },
        "escalations": {
            "total": esc.get("n", 0) or 0,
            "open": esc.get("open_n", 0) or 0,
            "kate_open": esc.get("kate_open", 0) or 0,
            "clinician_total": esc.get("clin_n", 0) or 0,
            "clinician_open": esc.get("clin_open", 0) or 0,
            "clinician_overdue": esc.get("clin_overdue", 0) or 0,
        },
        "human_minutes_logged": round(ht.get("logged", 0) or 0, 1),
        "human_minutes_default_assumed": round(ht.get("assumed", 0) or 0, 1),
        "clinician_minutes_partner": round(clin_minutes.get("m", 0) or 0, 1),
        "conversations_with_no_escalation": no_esc.get("n", 0),
        "conversations_verified_with_no_escalation": verified_no_esc.get("n", 0),
    }
