"""Stdlib HTTP dashboard and Kate's work surface.  Binds 127.0.0.1 only.  No external assets, no auth
(local prototype).  The simulated phone here calls the SAME engine.handle_inbound the Twilio webhook calls.

Pages:   GET /                                  Operations (version 3): active conversations, escalations by path/owner, data connection,
                                                 patients/orders by state, map, first-text variants, spend, controls
         GET /conversation/<id>                 Conversation (version 3): one phone-like thread, Flag under each reply, "why" on click
         GET /legacy                            the round-two dashboard (kept for reference)
JSON:    GET  /api/ops                          everything the Operations page shows
         POST /api/settings {opener_disclosure, opener_sites, max_spend_usd_per_day, clinical_handoff, resolver_mode, clinical_pause_days}
         GET  /where/<token>                   version 4: consent-based one-time location share page (single use, expiring)
         POST /api/location {token, lat, lon}  the page posts here; nearest sites are then offered from that point
JSON:    GET  /api/state                        roll-up, pause, feed freshness, queues, conversations, config, decisions
         GET  /api/conversation/<id>            thread (with decision records), orders, preferences, escalations, events
         POST /api/pause {reason} | /api/resume | /api/tick | /api/advance {hours}
         POST /api/sim/inbound {phone, body}    simulated patient text (same code path as the webhook)
         POST /api/escalations/<id>/acknowledge | /resolve {resolution, minutes, next_step}
         POST /api/import/updates {feed} | /api/link_click {conversation_id} | /api/human_time {activity, minutes}
         POST /api/threshold {days}             overdue threshold, 15..45, then rescreen
         POST /api/preference {patient_id, key, value}   Kate confirms/overrides a preference
         GET  /api/feedback | GET /api/feedback/<id> | POST /api/feedback {message_id, label, should_have, notes}
         POST /api/feedback/<id>/status {status, linked_ref, verification_note}
         POST /api/feedback/<id>/export         writes feedback/exports/task-NNN-*.md (synthetic-only guard)
         GET  /api/costs                        measured (this database) vs assumed (costs/workload_scenarios.json)
         GET  /api/decisions                    unresolved product decisions + latest eval failures
Webhook: POST /webhook/twilio                   form-encoded; signature-checked when the Twilio adapter is active
"""
from __future__ import annotations

import glob
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Dict

from . import feedback as fb
from . import feed_integrity as _feedi
from . import facts as _facts, referrals as _refs, scheduling as _sched, improve as _improve
from .db import get_setting, set_setting, rows, row
from .metrics import summary
from .models import ESCALATION_REASONS

_LOCK = Lock()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_payload(engine) -> Dict:
    now = engine.now()
    latest_feed = row(engine.conn, "SELECT MAX(generated_at) g, MAX(imported_at) i FROM feed_imports") or {}
    convs = rows(engine.conn, "SELECT c.id, c.state, c.outreach_attempts, c.next_action, c.next_action_at, c.next_action_reason, c.model_calls, "
                              "c.outbound_count, c.inbound_count, c.episode, p.display_name, p.phone, p.home_town, p.synthetic, "
                              "(SELECT GROUP_CONCAT(o.state) FROM orders o WHERE o.patient_id=p.id) order_states, "
                              "(SELECT COUNT(*) FROM escalations e WHERE e.conversation_id=c.id AND e.status='open') open_items "
                              "FROM conversations c JOIN patients p ON p.id=c.patient_id ORDER BY c.id")
    escs = rows(engine.conn, "SELECT e.*, p.display_name FROM escalations e JOIN conversations c ON c.id=e.conversation_id "
                             "JOIN patients p ON p.id=c.patient_id WHERE e.status='open' ORDER BY e.overdue DESC, e.opened_at")
    for e in escs:
        e["label"] = ESCALATION_REASONS.get(e["reason"], e["reason"])
    events = rows(engine.conn, "SELECT * FROM events ORDER BY id DESC LIMIT 40")
    return {
        "now": now.isoformat(),
        "paused": engine.paused(),
        "feed": {"latest_generated_at": latest_feed.get("g"), "latest_imported_at": latest_feed.get("i"),
                 "stale": engine.feed_is_stale(), "stale_after_hours": engine.policy.stale_feed_hours},
        "adapters": {"model": engine.model.name, "model_id": getattr(engine.model, "model", "mock-rules-v1"),
                     "model_simulated": bool(getattr(engine.model, "simulated", True)),
                     "messaging": engine.messaging.name, "messaging_simulated": bool(engine.messaging.simulated),
                     "twilio_send_enabled": bool(getattr(engine.messaging, "send_enabled", False))},
        "config": {"overdue_threshold_days": engine.policy.min_order_age_days, "threshold_range": [15, 45],
                   "software": {"ocp_version": fb.__version__, "git_commit": fb.git_commit()}},
        "directory": {"partner": engine.directory.partner_name, "verified_sites": len(engine.directory.sites(now)),
                      "rejected_sites": engine.directory.rejected(now)},
        "summary": summary(engine.conn),
        "conversations": convs,
        "kate_queue": [e for e in escs if e["queue"] == "kate"],
        "clinician_queue": [e for e in escs if e["queue"] == "clinician"],
        "feedback": fb.list_all(engine),
        "events": events,
    }


def conversation_payload(engine, cid: int) -> Dict:
    conv = row(engine.conn, "SELECT c.*, p.display_name, p.phone, p.synthetic, p.id patient_id FROM conversations c JOIN patients p ON p.id=c.patient_id "
                            "WHERE c.id=?", (cid,))
    if not conv:
        return {}
    msgs = rows(engine.conn, "SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
    for m in msgs:
        if m.get("decision"):
            try:
                m["decision"] = json.loads(m["decision"])
            except ValueError:
                pass
    orders = rows(engine.conn, "SELECT o.*, (SELECT GROUP_CONCAT(l.test_name||':'||l.status) FROM order_lines l "
                               "WHERE l.order_id=o.id) lines FROM orders o WHERE o.patient_id=? ORDER BY o.id", (conv["patient_id"],))
    prefs = rows(engine.conn, "SELECT * FROM preferences WHERE patient_id=? ORDER BY id", (conv["patient_id"],))
    events = rows(engine.conn, "SELECT * FROM events WHERE conversation_id=? ORDER BY id", (cid,))
    escs = rows(engine.conn, "SELECT * FROM escalations WHERE conversation_id=? ORDER BY id", (cid,))
    fbs = [f for f in fb.list_all(engine) if f["conversation_id"] == cid and not f.get("archived")]
    portal_msgs = rows(engine.conn, "SELECT id, subject, body, status, adapter, created_at, sent_at, provider FROM portal_messages WHERE conversation_id=? ORDER BY id", (cid,))
    locations = rows(engine.conn, "SELECT source, label, created_at, expires_at FROM patient_locations WHERE patient_id=? ORDER BY id", (conv["patient_id"],))
    return {"conversation": conv, "messages": msgs, "orders": orders, "preferences": prefs, "events": events,
            "escalations": escs, "feedback": fbs, "portal_messages": portal_msgs, "locations": locations}


def costs_payload(engine) -> Dict:
    s = summary(engine.conn)
    convs = s["conversations"] or 1
    measured = {
        "source": "this database (synthetic replay); ratios per conversation, not per patient-month",
        "outbound_per_conversation": round(s["sms"]["provider_accepted_messages"] / convs, 2),
        "inbound_per_conversation": round(s["sms"]["inbound_messages"] / convs, 2),
        "segments_per_outbound": round(s["sms"]["provider_accepted_segments"] / max(1, s["sms"]["provider_accepted_messages"]), 2),
        "model_attempts_per_inbound": round(s["model"]["attempts_recorded"] / max(1, s["sms"]["inbound_messages"]), 2),
        "avg_input_tokens_per_attempt_MOCK_ESTIMATE": round(s["model"]["input_tokens"] / max(1, s["model"]["attempts_recorded"])),
        "kate_escalations_per_conversation": round((s["escalations"]["total"] - s["escalations"]["clinician_total"]) / convs, 2),
        "clinician_escalations_per_conversation": round(s["escalations"]["clinician_total"] / convs, 2),
        "human_minutes_logged": s["human_minutes_logged"], "human_minutes_default_assumed": s["human_minutes_default_assumed"],
        "unresolved_orders": s["orders_by_state"].get("unresolved", 0),
        "verified_completions": s["verified_completions"], "completed_external_attested": s.get("completed_external", 0),
    }
    try:
        scen = json.load(open(os.path.join(ROOT, "costs", "workload_scenarios.json")))["scenarios"]
    except Exception:  # noqa: BLE001
        scen = {}
    return {"measured": measured, "assumed_scenarios": scen,
            "note": "Measured ratios come from scripted synthetic fixtures chosen to exercise failure paths; they are not "
                    "forecasts.  Assumed scenarios are editable in costs/workload_scenarios.json; run costs/workload_scenarios.py."}


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def ops_payload(engine) -> Dict:
    """Operations page: what is live now, what is waiting on a person, the data connection, the cohort by state,
    the map, first-text variants, and spend.  Every number is read back from rows the engine wrote."""
    now = engine.now()
    s = summary(engine.conn)
    active_states = ("outreach_sent", "engaged", "plan_agreed")
    active = rows(engine.conn, "SELECT c.id, c.state, c.next_action, c.next_action_at, c.outreach_attempts, p.display_name, p.home_town, "
                               "(SELECT body FROM messages m WHERE m.conversation_id=c.id AND m.status NOT IN ('cancelled','suppressed') ORDER BY m.id DESC LIMIT 1) last_body, "
                               "(SELECT direction FROM messages m WHERE m.conversation_id=c.id AND m.status NOT IN ('cancelled','suppressed') ORDER BY m.id DESC LIMIT 1) last_dir, "
                               "(SELECT MAX(created_at) FROM messages m WHERE m.conversation_id=c.id) last_at "
                               "FROM conversations c JOIN patients p ON p.id=c.patient_id WHERE c.state IN (%s) ORDER BY last_at DESC" % ",".join("?" * len(active_states)), active_states)
    escs = rows(engine.conn, "SELECT e.id, e.reason, e.queue, e.assigned_to, e.due_at, e.overdue, e.handoff_status, e.opened_at, e.summary, e.conversation_id, p.display_name "
                             "FROM escalations e JOIN conversations c ON c.id=e.conversation_id JOIN patients p ON p.id=c.patient_id "
                             "WHERE e.status='open' ORDER BY e.overdue DESC, e.due_at")
    for e in escs:
        e["label"] = ESCALATION_REASONS.get(e["reason"], e["reason"])
        e["due_passed"] = bool(e["due_at"] and e["due_at"] < now.strftime("%Y-%m-%dT%H:%M:%S"))
    by_path: Dict = {}
    for e in escs:
        by_path.setdefault(e["queue"], {}).setdefault(e["assigned_to"] or "unassigned", []).append(e)
    latest = row(engine.conn, "SELECT MAX(generated_at) g, MAX(imported_at) i, COUNT(*) n FROM feed_imports") or {}
    next_pickup = None
    if latest.get("g"):
        from datetime import datetime as _dt, timedelta as _td
        try:
            next_pickup = (_dt.fromisoformat(latest["g"][:19]) + _td(hours=24)).isoformat()
        except ValueError:
            next_pickup = None
    connection = {"kind": "file (partner export, imported by the application)", "last_received": latest.get("g"), "last_imported": latest.get("i"),
                  "imports": latest.get("n", 0), "next_pickup": next_pickup, "stale_after_hours": engine.policy.stale_feed_hours,
                  "late": bool(engine.feed_is_stale()), "note": "In the pilot this becomes an SFTP/FHIR pickup job; 'late' means the newest file is older than the stale threshold."}
    # map: patients by town, sites, mobile route slot
    towns = engine.directory.towns
    pts = rows(engine.conn, "SELECT p.home_town town, COUNT(*) n, SUM(CASE WHEN c.state IN ('outreach_sent','engaged','plan_agreed') THEN 1 ELSE 0 END) active "
                            "FROM patients p LEFT JOIN conversations c ON c.patient_id=p.id GROUP BY p.home_town")
    map_patients = [{"town": r["town"], "n": r["n"], "active": r["active"] or 0, "lat": towns.get(r["town"], {}).get("lat"), "lon": towns.get(r["town"], {}).get("lon")} for r in pts]
    sites = [{"id": x["id"], "name": x["name"], "town": x.get("town"), "lat": x.get("lat"), "lon": x.get("lon"), "services": x.get("services", []),
              "valid": engine.directory.site(x["id"], now) is not None,
              "sent_to": row(engine.conn, "SELECT COUNT(*) n FROM conversations WHERE agreed_site_id=?", (x["id"],))["n"]} for x in engine.directory._sites]
    # first-text variants: only conversations whose FIRST text was actually sent count as exposures; the key is the
    # realized variant (disclosure as rendered, sites named, visit date, writer), not the assignment (V3-7)
    convs = rows(engine.conn, "SELECT c.id, c.opener_variant, c.state, c.agreed_date, c.inbound_count, c.created_at, "
                              "(SELECT MIN(m.sent_at) FROM messages m WHERE m.conversation_id=c.id AND m.direction='outbound' AND m.status='sent' AND m.template_id='outreach_initial') first_out, "
                              "(SELECT MIN(m.created_at) FROM messages m WHERE m.conversation_id=c.id AND m.direction='inbound') first_in, "
                              "(SELECT COUNT(*) FROM events ev WHERE ev.conversation_id=c.id AND ev.kind='conversation_state' AND ev.detail LIKE '%plan_agreed%') planned_ev, "
                              "(SELECT COUNT(*) FROM orders o WHERE o.patient_id=c.patient_id AND o.state IN ('verified_complete','completed_external')) completed "
                              "FROM conversations c WHERE c.opener_variant IS NOT NULL")
    variants: Dict = {}
    from datetime import datetime as _dt2
    for c in convs:
        if not c["first_out"]:
            continue                                   # assigned or queued, never sent: not an exposure
        v = json.loads(c["opener_variant"])
        key = "disclosure=%s · sites=%s · visit_date=%s · writer=%s" % (v.get("disclosure_realized", v.get("disclosure")), v.get("sites_named", v.get("sites")),
                                                                        "yes" if v.get("visit_date_present") else "no", v.get("writer", v.get("composer")))
        g = variants.setdefault(key, {"variant": v, "sent": 0, "replied": 0, "planned": 0, "completed": 0, "hours_to_reply": []})
        g["sent"] += 1
        if c["inbound_count"] and c["first_in"] and c["first_in"] >= c["first_out"]:
            g["replied"] += 1
            g["hours_to_reply"].append(round((_dt2.fromisoformat(c["first_in"]) - _dt2.fromisoformat(c["first_out"])).total_seconds() / 3600.0, 1))
        if c["state"] == "plan_agreed" or c["agreed_date"] or c["planned_ev"]:
            g["planned"] += 1
        if c["completed"]:
            g["completed"] += 1
    for g in variants.values():
        h = sorted(g.pop("hours_to_reply"))
        n = len(h)
        g["median_hours_to_first_reply"] = None if not n else (h[n // 2] if n % 2 else round((h[n // 2 - 1] + h[n // 2]) / 2.0, 1))
        g["reply_rate_pct"] = _pct(g["replied"], g["sent"]); g["plan_rate_pct"] = _pct(g["planned"], g["sent"]); g["completion_rate_pct"] = _pct(g["completed"], g["sent"])
        g["completion_definition"] = "at least one of the patient's targeted orders verified complete or attested external"
    comp = row(engine.conn, "SELECT SUM(CASE WHEN composer='template' THEN 1 ELSE 0 END) tpl, SUM(CASE WHEN composer='fact' THEN 1 ELSE 0 END) fact, "
                            "SUM(CASE WHEN composer='anthropic' THEN 1 ELSE 0 END) live, COUNT(*) n FROM messages WHERE direction='outbound' AND status='sent'") or {}
    refused = row(engine.conn, "SELECT COUNT(*) n FROM events WHERE kind='composer_refused'") or {}
    spend = {"last_24h_usd": round(engine.spend_last_24h_usd(), 4), "cap_usd_per_day": engine.policy.max_spend_usd_per_day,
             "cap_reached": engine.spend_cap_exceeded(),
             "by_purpose": {r["purpose"]: {"calls": r["n"], "usd": round(r["c"] or 0, 4)} for r in rows(engine.conn, "SELECT purpose, COUNT(*) n, SUM(cost_usd) c FROM model_calls WHERE simulated=0 GROUP BY purpose")}}
    # version 4: automation — how much reached a person, and why not
    n_conv = max(1, s["conversations"])
    ev = {r["kind"]: r["n"] for r in rows(engine.conn, "SELECT kind, COUNT(*) n FROM events WHERE kind IN ('clinical_portal_referral','clinical_self_served','emergency_wording',"
                                                        "'portal_relay_sent','resolver_decision','resolver_failed','menu_digit_decided','location_recorded','planner_shadow') GROUP BY kind")}
    res = rows(engine.conn, "SELECT detail FROM events WHERE kind='resolver_decision'")
    res_exec = sum(1 for r in res if json.loads(r["detail"]).get("executed"))
    res_dis = sum(1 for r in res if json.loads(r["detail"]).get("verdict") == "disagree")
    plan = rows(engine.conn, "SELECT detail FROM events WHERE kind='planner_shadow'")
    plan_agree = sum(1 for r in plan if json.loads(r["detail"]).get("agree"))
    plan_dis = [json.loads(r["detail"]) for r in plan if not json.loads(r["detail"]).get("agree")][-8:]
    clin_items = row(engine.conn, "SELECT COUNT(*) n FROM escalations WHERE queue='clinician'")["n"]
    kate_items = row(engine.conn, "SELECT COUNT(*) n FROM escalations WHERE queue='kate'")["n"]
    automation = {
        "clinical_handoff": engine.policy.clinical_handoff, "portal": engine.directory.portal(now),
        "clinician_items_per_100_conversations": round(100.0 * clin_items / n_conv, 1), "kate_items_per_100_conversations": round(100.0 * kate_items / n_conv, 1),
        "portal_referrals": ev.get("clinical_portal_referral", 0), "prep_answered_from_approved_text": ev.get("clinical_self_served", 0),
        "portal_relays_sent": ev.get("portal_relay_sent", 0), "emergency_texts": ev.get("emergency_wording", 0),
        "resolver": {"mode": engine.policy.resolver_mode, "reviewer": engine.policy.resolver_reviewer,
                     "reviewer_is_model": not getattr(engine.reviewer, "simulated", True), "reviewer_label": ("%s (LIVE model)" % getattr(engine.reviewer, "model", "?")) if not getattr(engine.reviewer, "simulated", True) else "rules (not a model)",
                     "attempted": len(res), "executed": res_exec,
                     "disagreements": res_dis, "failed": ev.get("resolver_failed", 0)},
        "menu_digits_decided": ev.get("menu_digit_decided", 0), "locations_recorded": ev.get("location_recorded", 0),
        "planner_shadow": {"compared": len(plan), "agree": plan_agree, "agreement_pct": _pct(plan_agree, len(plan)),
                           "disposition_agree": sum(1 for r in plan if json.loads(r["detail"]).get("disposition_agree")), "recent_disagreements": plan_dis,
                           "note": "agreement is on the exact action or a narrow same-work family; it is not an accuracy measure until disagreements are adjudicated"},
        "clinical_route": engine.clinical_route(),
        "note": "clinician-facing items per 100 conversations is the number a health system will ask for; target under 5",
    }
    # version 5: referrals, facts, booking funnel, measures
    refs = _refs.payload(engine)
    facts_summary = {r["status"]: r["n"] for r in rows(engine.conn, "SELECT status, COUNT(*) n FROM fact_cards GROUP BY status")}
    chart_minutes = row(engine.conn, "SELECT COALESCE(SUM(minutes),0) m, COUNT(*) n FROM human_time WHERE activity LIKE 'chart_review:%'") or {}
    turns = rows(engine.conn, "SELECT c.id, c.inbound_count, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id AND m.direction='outbound' AND m.status='sent') outn, "
                              "(SELECT COUNT(*) FROM feedback f WHERE f.conversation_id=c.id) flags FROM conversations c")
    replied = [t for t in turns if t["inbound_count"]]
    experience = {"conversations": len(turns), "replied": len(replied), "reply_rate_pct": _pct(len(replied), len(turns)),
                  "median_outbound_per_replied_conversation": (sorted(t["outn"] for t in replied)[len(replied) // 2] if replied else None),
                  "flags_per_100_conversations": round(100.0 * sum(t["flags"] for t in turns) / max(1, len(turns)), 1),
                  "note": "proxies on synthetic fixtures; real patient experience needs real replies and a survey"}
    labeled = rows(engine.conn, "SELECT detail FROM events WHERE kind='labeled_escalation_check'")
    lab = {"cases": len(labeled), "missed": sum(1 for l in labeled if json.loads(l["detail"]).get("missed")), "unnecessary": sum(1 for l in labeled if json.loads(l["detail"]).get("unnecessary"))}
    measures = {"experience": experience, "completion_funnel": _sched.funnel(engine.conn),
                "escalation": {"clinician_facing_items_per_100": automation["clinician_items_per_100_conversations"], "referrals_total": len(refs["referrals"]),
                               "referrals_with_missing_rationale": refs["missing_rationale"], "labeled_cases": lab,
                               "note": "unnecessary = a referral whose question an approved documented card could have answered; missed = labeled clinical wording that produced no referral; both from labeled scenario checks, not a claim about real patients"},
                "staff_workload": {"human_minutes_logged": s["human_minutes_logged"], "human_minutes_default_assumed": s["human_minutes_default_assumed"],
                                   "chart_review_minutes": round(chart_minutes.get("m") or 0, 1), "chart_reviews": chart_minutes.get("n") or 0},
                "principle": "not optimized solely for fewer clinical referrals: a missed escalation costs more than an unnecessary one"}
    return {
        "now": now.isoformat(), "paused": engine.paused(), "automation": automation,
        "v5": {"referrals": {k: refs[k] for k in ("by_state", "by_reason", "by_team", "overdue", "duplicates", "failed", "ambiguous_routing", "missing_rationale")}, "referrals_total": len(refs["referrals"]),
               "facts": facts_summary, "funnel": _sched.funnel(engine.conn), "measures": measures,
               "operator_role": engine.policy.operator_role, "scheduler": {"adapter": engine.scheduler.name, "simulated": engine.scheduler.simulated}},
        "adapters": {"model": engine.model.name, "model_id": getattr(engine.model, "model", "mock-rules-v1"), "model_simulated": bool(getattr(engine.model, "simulated", True)),
                     "composer": getattr(engine.composer, "name", "template"), "composer_model": getattr(engine.composer, "model", None),
                     "composer_simulated": bool(getattr(engine.composer, "simulated", True)),
                     "messaging": engine.messaging.name, "messaging_simulated": bool(engine.messaging.simulated)},
        "active": active, "active_by_state": {k: v for k, v in s["conversations_by_state"].items()},
        "escalations": escs, "escalations_by_path": by_path,
        "connection": connection,
        "feed": _feedi.summary(engine),
        "patients_by_state": s["conversations_by_state"], "orders_by_state": s["orders_by_state"], "patients": s["patients"],
        "map": {"patients": map_patients, "sites": sites, "mobile_routes": engine.directory.mobile_routes, "towns": towns},
        "variants": variants,
        "settings": {"opener_disclosure": get_setting(engine.conn, "opener_disclosure", engine.policy.opener_disclosure),
                     "opener_sites": int(get_setting(engine.conn, "opener_sites", str(engine.policy.opener_sites))),
                     "overdue_threshold_days": engine.policy.min_order_age_days,
                     "clinical_handoff": engine.policy.clinical_handoff, "resolver_mode": engine.policy.resolver_mode,
                     "clinical_pause_days": engine.policy.clinical_pause_days},
        "composer": {"sent_by_writer": {"template": comp.get("tpl") or 0, "fact": comp.get("fact") or 0, "anthropic": comp.get("live") or 0, "total": comp.get("n") or 0},
                     "refusals": refused.get("n", 0)},
        "spend": spend,
        "feedback_open": s["feedback"]["open"],
        "software": {"ocp_version": fb.__version__, "git_commit": fb.git_commit()},
    }


def decisions_payload() -> Dict:
    try:
        decisions = json.load(open(os.path.join(ROOT, "data", "open_decisions.json")))
    except Exception:  # noqa: BLE001
        decisions = {"decisions": []}
    evals = []
    for path in sorted(glob.glob(os.path.join(ROOT, "eval", "results", "*.json")))[-4:]:
        try:
            d = json.load(open(path))
            sm = d["summary"]
            evals.append({"file": os.path.basename(path), "label": sm["label"], "set": sm["set"], "cases": sm["cases"],
                          "passed": sm["passed"], "failed": sm["failed"], "errors": sm["errors"],
                          "failures": [{"id": r["id"], "text": r["text"][:70], "why": r.get("why") or r.get("error")} for r in d["results"] if r["status"] != "pass"][:12]})
        except Exception:  # noqa: BLE001
            continue
    return {"decisions": decisions.get("decisions", []), "eval_runs": evals}


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, code: int, payload) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            self._raw = raw                     # Sept 23: the feed endpoint validates the bytes as sent (encoding, size, hash)
            if "application/x-www-form-urlencoded" in self.headers.get("Content-Type", ""):
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
            try:
                return json.loads(raw.decode() or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            with _LOCK:
                try:
                    if path in ("/", "/ops", "/legacy") or path.startswith("/conversation/"):
                        body = (OPS_HTML if path in ("/", "/ops") else (DASHBOARD_HTML if path == "/legacy" else CONVERSATION_HTML)).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path.startswith("/where/"):
                        token = path.rsplit("/", 1)[1]
                        lk = row(engine.conn, "SELECT token, used_at, expires_at FROM location_links WHERE token=?", (token,))
                        valid = bool(lk) and not lk["used_at"] and lk["expires_at"] >= engine.now().strftime("%Y-%m-%dT%H:%M:%S")
                        body = (LOCATION_HTML.replace("__TOKEN__", token) if valid else LOCATION_EXPIRED_HTML).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path in ("/referrals", "/facts", "/improve") or path.startswith("/referrals/") or path.startswith("/facts/"):
                        html = REFERRALS_HTML if path.startswith("/referrals") else (FACTS_HTML if path.startswith("/facts") else IMPROVE_HTML)
                        body = html.encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif path == "/api/referrals":
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                        self._json(200, _refs.payload(engine, {k: v[0] for k, v in qs.items()}))
                    elif path.startswith("/api/referrals/"):
                        self._json(200, _refs.detail(engine, int(path.split("/")[3])) or {})
                    elif path == "/api/facts":
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                        self._json(200, {"cards": _facts.list_cards(engine, status=(qs.get("status") or [None])[0], kind=(qs.get("kind") or [None])[0],
                                                                    patient_id=int(qs["patient_id"][0]) if qs.get("patient_id") else None),
                                         "role": engine.policy.operator_role, "kinds": _facts.FACT_KINDS, "statuses": _facts.STATUSES,
                                         "notes": rows(engine.conn, "SELECT n.note_id, n.author, n.authored_at, n.access_basis, p.display_name, p.id patient_id FROM clinical_notes n JOIN patients p ON p.id=n.patient_id ORDER BY n.authored_at DESC")})
                    elif path.startswith("/api/facts/"):
                        self._json(200, _facts.card_detail(engine, int(path.split("/")[3])) or {})
                    elif path == "/api/improve":
                        self._json(200, _improve.payload(engine))
                    elif path.startswith("/api/eval_cases/"):
                        self._json(200, _improve.get_case(engine, int(path.split("/")[3])) or {})
                    elif path == "/api/ops":
                        self._json(200, ops_payload(engine))
                    elif path == "/api/feed":
                        self._json(200, _feedi.payload(engine))
                    elif path == "/api/state":
                        self._json(200, state_payload(engine))
                    elif path.startswith("/api/conversation/"):
                        self._json(200, conversation_payload(engine, int(path.rsplit("/", 1)[1])))
                    elif path == "/api/feedback":
                        self._json(200, {"feedback": fb.list_all(engine)})
                    elif path.startswith("/api/feedback/"):
                        self._json(200, fb.get(engine, int(path.split("/")[3])) or {})
                    elif path == "/api/costs":
                        self._json(200, costs_payload(engine))
                    elif path == "/api/decisions":
                        self._json(200, decisions_payload())
                    else:
                        self._json(404, {"error": "not found"})
                except Exception as e:  # noqa: BLE001
                    self._json(500, {"error": "%s: %s" % (e.__class__.__name__, e)})

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            data = self._read_body()
            if not isinstance(data, dict):
                data = {"_body": data}                     # a JSON array or scalar body: only the feed endpoint accepts it, from the raw bytes
            with _LOCK:
                try:
                    if path == "/api/pause":
                        engine.pause(str(data.get("reason") or "manual"), actor=str(data.get("actor") or "kate"))
                        self._json(200, {"ok": True, "paused": engine.paused()})
                    elif path == "/api/resume":
                        engine.resume(actor=str(data.get("actor") or "kate"))
                        self._json(200, {"ok": True, "paused": engine.paused()})
                    elif path == "/api/tick":
                        self._json(200, engine.tick())
                    elif path == "/api/advance":
                        dt = engine.advance(hours=float(data.get("hours") or 0))
                        self._json(200, {"ok": True, "now": dt.isoformat()})
                    elif path == "/api/sim/inbound":
                        self._json(200, engine.handle_inbound(str(data.get("phone") or ""), str(data.get("body") or ""),
                                                             provider_message_id=data.get("provider_message_id")))
                    elif path.startswith("/api/escalations/") and path.endswith("/resolve"):
                        eid = int(path.split("/")[3])
                        minutes = data.get("minutes")
                        self._json(200, engine.resolve_escalation(eid, str(data.get("actor") or "kate"), str(data.get("resolution") or ""),
                                                                  float(minutes) if minutes not in (None, "") else None,
                                                                  str(data.get("next_step") or "resume")))
                    elif path.startswith("/api/escalations/") and path.endswith("/acknowledge"):
                        self._json(200, engine.acknowledge_escalation(int(path.split("/")[3]), str(data.get("actor") or "kate")))
                    elif path == "/api/import/updates":
                        # Sept 23: the API is an arrival path; it goes through validation like a pickup or a push
                        self._json(200, engine.receive_feed(data.get("feed") or data, source="api", source_name="api:import/updates"))
                    elif path == "/api/feed/receive":
                        name = self.headers.get("X-Feed-Name") or (data.get("name") if isinstance(data, dict) else None) or "push"
                        self._json(200, engine.receive_feed(self._raw if self._raw else data, source="push", source_name=str(name)))
                    elif path.startswith("/api/feed/alerts/") and path.endswith("/acknowledge"):
                        self._json(200, _feedi.acknowledge(engine, int(path.split("/")[4]), str(data.get("actor") or ""), str(data.get("note") or "")))
                    elif path.startswith("/api/feed/alerts/") and path.endswith("/resolve"):
                        self._json(200, _feedi.resolve(engine, int(path.split("/")[4]), str(data.get("actor") or ""), str(data.get("resolution") or "")))
                    elif path.startswith("/api/feed/receipts/") and path.endswith("/reprocess"):
                        self._json(200, _feedi.reprocess(engine, int(path.split("/")[4]), str(data.get("actor") or "")))
                    elif path == "/api/feed/unblock":
                        self._json(200, _feedi.unblock(engine, str(data.get("partner_id") or engine.directory.partner_id), str(data.get("kind") or "orders"), str(data.get("actor") or ""), str(data.get("note") or "")))
                    elif path == "/api/link_click":
                        self._json(200, engine.record_link_click(int(data.get("conversation_id"))))
                    elif path == "/api/human_time":
                        engine.record_human_time(str(data.get("actor") or "kate"), str(data.get("activity") or "review"), float(data.get("minutes") or 0))
                        self._json(200, {"ok": True})
                    elif path == "/api/session/fresh":
                        from .scenarios import load_orders_feed, SIM_START
                        self._json(200, engine.seed_session(load_orders_feed(), SIM_START, actor=str(data.get("actor") or "kate")))
                    elif path.startswith("/api/referrals/") and path.endswith("/resolve"):
                        self._json(200, _refs.resolve(engine, int(path.split("/")[3]), str(data.get("actor") or "kate"), str(data.get("role") or engine.policy.operator_role),
                                                      str(data.get("basis") or ""), str(data.get("authority") or "dashboard"), str(data.get("outcome") or "resolved")))
                    elif path.startswith("/api/referrals/") and path.endswith("/note"):
                        _refs.note(engine, int(path.split("/")[3]), str(data.get("actor") or "kate"), str(data.get("text") or "")); engine.conn.commit()
                        self._json(200, {"ok": True})
                    elif path.startswith("/api/referrals/") and path.endswith("/evidence"):
                        self._json(200, _refs.record_partner_evidence(engine, int(path.split("/")[3]), str(data.get("kind") or ""), str(data.get("actor") or "partner (entered by hand)"), data.get("detail") or {}))
                    elif path == "/api/notes/import":
                        r = _facts.import_notes(engine.conn, data.get("feed") or data, engine.now()); engine.conn.commit()
                        self._json(200, r)
                    elif path == "/api/facts/extract":
                        pid = int(data.get("patient_id"))
                        self._json(200, {"ok": True, "cards": _facts.rule_extract_rationale(engine, pid, actor=str(data.get("actor") or "kate"))})
                    elif path.startswith("/api/facts/") and path.endswith("/review"):
                        self._json(200, _facts.review(engine, int(path.split("/")[3]), str(data.get("actor") or "kate"), str(data.get("role") or engine.policy.operator_role),
                                                      str(data.get("action") or ""), data.get("statement"), str(data.get("note") or ""),
                                                      float(data["minutes"]) if data.get("minutes") not in (None, "") else None))
                    elif path.startswith("/api/facts/") and path.endswith("/flag"):
                        self._json(200, _improve.flag(engine, "fact_card", int(path.split("/")[3]), str(data.get("label") or "defect"), str(data.get("category") or "factual_support"),
                                                      str(data.get("should_have") or ""), str(data.get("notes") or ""), actor=str(data.get("actor") or "kate")))
                    elif path.startswith("/api/referrals/") and path.endswith("/flag"):
                        self._json(200, _improve.flag(engine, "referral", int(path.split("/")[3]), str(data.get("label") or "defect"), str(data.get("category") or "routing"),
                                                      str(data.get("should_have") or ""), str(data.get("notes") or ""), actor=str(data.get("actor") or "kate")))
                    elif path == "/api/eval_cases/from_feedback":
                        self._json(200, _improve.case_from_feedback(engine, int(data.get("feedback_id")), actor=str(data.get("actor") or "kate"), expect=data.get("expect")))
                    elif path.startswith("/api/eval_cases/") and path.endswith("/review"):
                        self._json(200, _improve.review_case(engine, int(path.split("/")[3]), str(data.get("actor") or "kate"), data.get("expect"), str(data.get("status") or "reviewed")))
                    elif path.startswith("/api/eval_cases/") and path.endswith("/export"):
                        self._json(200, {"ok": True, "path": os.path.relpath(_improve.export_case(engine, int(path.split("/")[3])), ROOT)})
                    elif path.startswith("/api/eval_cases/") and path.endswith("/run"):
                        c = _improve.get_case(engine, int(path.split("/")[3]))
                        self._json(200, _improve.run_case(c["case"]) if c else {"error": "no such case"})
                    elif path == "/api/candidates":
                        self._json(200, _improve.propose_change(engine, str(data.get("title") or ""), str(data.get("description") or ""), [int(x) for x in (data.get("addresses") or [])],
                                                                [int(x) for x in (data.get("case_ids") or [])], actor=str(data.get("actor") or "kate"), change_ref=str(data.get("change_ref") or "")))
                    elif path.startswith("/api/candidates/") and path.endswith("/evaluate"):
                        self._json(200, _improve.evaluate_change(engine, int(path.split("/")[3]), actor=str(data.get("actor") or "kate")))
                    elif path.startswith("/api/candidates/") and path.endswith("/decide"):
                        self._json(200, _improve.decide_change(engine, int(path.split("/")[3]), str(data.get("action") or ""), str(data.get("actor") or "kate"), str(data.get("note") or ""), str(data.get("tag") or "")))
                    elif path == "/api/location":
                        self._json(200, engine.record_shared_location(str(data.get("token") or ""), data.get("lat"), data.get("lon")))
                    elif path == "/api/settings":
                        out = {}
                        if "clinical_handoff" in data:
                            v = str(data["clinical_handoff"])
                            from .models import CLINICAL_HANDOFF_MODES, RESOLVER_MODES
                            if v not in CLINICAL_HANDOFF_MODES:
                                raise ValueError("clinical_handoff must be one of %s" % (CLINICAL_HANDOFF_MODES,))
                            engine.policy.clinical_handoff = v; set_setting(engine.conn, "clinical_handoff", v); out["clinical_handoff"] = v
                        if "resolver_mode" in data:
                            v = str(data["resolver_mode"])
                            from .models import RESOLVER_MODES
                            if v not in RESOLVER_MODES:
                                raise ValueError("resolver_mode must be one of %s" % (RESOLVER_MODES,))
                            engine.policy.resolver_mode = v; set_setting(engine.conn, "resolver_mode", v); out["resolver_mode"] = v
                        if "operator_role" in data:
                            v = str(data["operator_role"])
                            if v not in ("operator", "clinical_reviewer"):
                                raise ValueError("operator_role must be operator | clinical_reviewer")
                            engine.policy.operator_role = v; set_setting(engine.conn, "operator_role", v); out["operator_role"] = v
                        if "clinical_pause_days" in data:
                            engine.policy.clinical_pause_days = max(0, min(30, int(data["clinical_pause_days"]))); out["clinical_pause_days"] = engine.policy.clinical_pause_days
                        if "opener_disclosure" in data:
                            v = str(data["opener_disclosure"])
                            if v not in ("none", "short", "ab"):
                                raise ValueError("opener_disclosure must be none | short | ab")
                            set_setting(engine.conn, "opener_disclosure", v); out["opener_disclosure"] = v
                        if "opener_sites" in data:
                            n = int(data["opener_sites"])
                            if n not in (1, 2, 3):
                                raise ValueError("opener_sites must be 1, 2 or 3")
                            set_setting(engine.conn, "opener_sites", str(n)); out["opener_sites"] = n
                        if "max_spend_usd_per_day" in data:
                            engine.policy.max_spend_usd_per_day = float(data["max_spend_usd_per_day"]); out["max_spend_usd_per_day"] = engine.policy.max_spend_usd_per_day
                        from .db import log_event as _le
                        _le(engine.conn, engine.now().strftime("%Y-%m-%dT%H:%M:%S"), str(data.get("actor") or "kate"), "settings_changed", detail=out)
                        engine.conn.commit()
                        self._json(200, {"ok": True, **out})
                    elif path == "/api/threshold":
                        days = engine.set_overdue_threshold(int(data.get("days")), actor=str(data.get("actor") or "kate"))
                        r = engine.rescreen()
                        self._json(200, {"ok": True, "overdue_threshold_days": days, "rescreen": r})
                    elif path == "/api/preference":
                        self._json(200, engine.set_preference(int(data.get("patient_id")), str(data.get("key")), data.get("value"),
                                                              actor=str(data.get("actor") or "kate")))
                    elif path == "/api/feedback":
                        if data.get("category"):
                            self._json(200, _improve.flag(engine, "message", int(data.get("message_id")), str(data.get("label") or ""), str(data.get("category")),
                                                          str(data.get("should_have") or ""), str(data.get("notes") or ""), actor=str(data.get("actor") or "kate")))
                        else:
                            self._json(200, fb.create(engine, int(data.get("message_id")), str(data.get("label") or ""),
                                                      str(data.get("should_have") or ""), str(data.get("notes") or ""),
                                                      actor=str(data.get("actor") or "kate")))
                    elif path.startswith("/api/feedback/") and path.endswith("/status"):
                        self._json(200, fb.update_status(engine, int(path.split("/")[3]), str(data.get("status") or "open"),
                                                         str(data.get("linked_ref") or ""), str(data.get("verification_note") or ""),
                                                         actor=str(data.get("actor") or "kate")))
                    elif path.startswith("/api/feedback/") and path.endswith("/export"):
                        r = fb.export_task(engine, int(path.split("/")[3]))
                        self._json(200, {"ok": True, "path": os.path.relpath(r["path"], ROOT), "markdown": r["markdown"]})
                    elif path == "/webhook/twilio":
                        adapter = engine.messaging
                        if getattr(adapter, "name", "") == "twilio":
                            url = "http://%s%s" % (self.headers.get("Host", ""), self.path)
                            if not adapter.verify_signature(url, data, self.headers.get("X-Twilio-Signature", "")):
                                self._json(403, {"error": "bad signature"})
                                return
                        engine.handle_inbound(data.get("From", ""), data.get("Body", ""), provider_message_id=data.get("MessageSid"), provider="twilio")
                        body = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"
                        self.send_response(200)
                        self.send_header("Content-Type", "text/xml")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self._json(404, {"error": "not found"})
                except PermissionError as e:
                    self._json(403, {"error": str(e)})
                except Exception as e:  # noqa: BLE001 - surface to the UI, never crash the server
                    try:
                        engine.conn.rollback()
                    except Exception:  # noqa: BLE001
                        pass
                    self._json(500, {"error": "%s: %s" % (e.__class__.__name__, e)})
    return Handler


def serve(engine, host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(engine))
    print("StealthCo order-completion prototype - dashboard on http://%s:%d  (Ctrl-C to stop)" % (host, port))
    print("model adapter: %s%s | messaging: %s%s" % (
        engine.model.name, " [SIMULATED]" if engine.model.name == "mock" else " [LIVE]",
        engine.messaging.name, " [nothing sent]" if engine.messaging.simulated else " [REAL SMS]"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


DASHBOARD_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Order completion - Kate's work surface</title>
<style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;background:#f6f7f4;color:#1c1f1a}
header{background:#24352a;color:#fff;padding:10px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header b{font-size:16px} .tag{padding:2px 8px;border-radius:10px;font-size:12px;background:#3c5546}
.tag.warn{background:#a8541c}.tag.sim{background:#5b5b8a}
main{display:grid;grid-template-columns:1fr 1.1fr;gap:14px;padding:14px}
section{background:#fff;border:1px solid #dfe3da;border-radius:8px;padding:12px;margin-bottom:14px}
h2{font-size:13px;margin:0 0 8px;color:#24352a;text-transform:uppercase;letter-spacing:.04em}
h2 small{text-transform:none;letter-spacing:0;color:#667;font-weight:normal}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:8px}
.kpi{background:#f1f4ee;border-radius:6px;padding:8px}.kpi b{display:block;font-size:18px}.kpi span{font-size:11px;color:#556}
table{width:100%;border-collapse:collapse;font-size:12.5px}td,th{padding:4px 6px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
tr.sel{background:#eef5ea}button{font-size:12px;padding:4px 8px;border:1px solid #889;border-radius:5px;background:#fff;cursor:pointer}
button.p{background:#24352a;color:#fff;border-color:#24352a}button.flag{border-color:#a8541c;color:#a8541c;font-size:11px;padding:1px 6px}
input,textarea,select{font-size:12.5px;padding:4px;font-family:inherit}
.msgs{max-height:360px;overflow:auto;background:#f6f7f4;padding:8px;border-radius:6px}
.m{margin:4px 0;padding:6px 8px;border-radius:8px;max-width:88%;font-size:12.5px;position:relative}.in{background:#fff;border:1px solid #ddd}
.out{background:#dfeee0;margin-left:auto}.m small{display:block;color:#667;font-size:10.5px}.m .why{color:#445;font-size:10.5px;background:#f8fbf5;padding:3px 5px;border-radius:4px;margin-top:3px;white-space:pre-wrap}
.ev{font-family:Menlo,monospace;font-size:11px;max-height:220px;overflow:auto;white-space:pre-wrap}
.row{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:6px 0}
.simnote{background:#fff4e5;border:1px solid #e8c48a;padding:6px 8px;border-radius:6px;font-size:12px;margin-bottom:6px}
#fbform{display:none;background:#fff7f0;border:1px solid #e8c48a;padding:8px;border-radius:6px;margin-top:6px}
.status-open{color:#a8541c}.status-linked{color:#1b5aa6}.status-verified{color:#2f7d32}.status-dismissed{color:#888}
details summary{cursor:pointer;font-size:12px;color:#445}
</style></head><body>
<header><b>StealthCo · order completion (synthetic)</b>
<span id="clock" class="tag"></span><span id="pause" class="tag"></span><span id="feed" class="tag"></span><span id="adapters" class="tag sim"></span><span id="ver" class="tag"></span>
<span class="row"><button class="p" onclick="post('/api/tick')">Run scheduler tick</button>
<button onclick="post('/api/advance',{hours:24})">+24h</button><button onclick="post('/api/advance',{hours:1})">+1h</button>
<button onclick="post('/api/pause',{reason:prompt('Pause reason')||'manual'})">Pause outreach</button>
<button onclick="post('/api/resume')">Resume</button>
<button onclick="if(confirm('Start a fresh synthetic session? Conversations reset; feedback rows are kept and archived.'))post('/api/session/fresh')">Fresh synthetic session</button>
<label style="font-size:12px" title="Affects future screening only: orders already eligible stay eligible; ineligible orders are re-screened now.">Overdue threshold <input id="thr" type="number" min="15" max="45" style="width:50px"> days <button onclick="setThr()">apply</button> <i>(future screening only)</i></label></span></header>
<main>
<div>
<section><h2>Roll-up <small>(read from the database; simulated model and messaging)</small></h2><div class="kpis" id="kpis"></div><div id="states" style="font-size:12px"></div></section>
<section><h2>Kate's queue <small>(operational; a real person owes the patient a reply)</small></h2><table id="kq"></table></section>
<section><h2>Partner clinician queue <small>(SIMULATED handoff: a row in this database, nobody has been notified, until "accepted")</small></h2><table id="cq"></table></section>
<section><h2>Feedback <small>(Kate's interpretation, separate from captured evidence)</small></h2><table id="fbt"></table></section>
<section><h2>Conversations</h2><table id="convs"></table></section>
</div>
<div>
<section><h2>Thread <span id="thread-title"></span></h2>
<div class="simnote">Simulated phone: what you type here goes through the same code path as a real inbound text (engine.handle_inbound). Hover "why" under any automated message to see the decision that produced it; click Flag to record what should have happened.</div>
<div class="msgs" id="msgs"><i>Select a conversation.</i></div>
<div class="row"><input id="reply" placeholder="Type as the patient..." style="flex:1"><button class="p" onclick="sendSim()">Send as patient</button>
<button onclick="linkClick()">Simulate link click</button></div>
<div id="fbform"><b>Flag message #<span id="fbmid"></span></b> - <span id="fbbody" style="color:#555"></span><br>
<div class="row"><label>Label <select id="fblabel"><option value="defect">suspected defect</option><option value="preference">product preference</option><option value="question">question</option></select></label></div>
<div class="row"><textarea id="fbshould" placeholder="What should have happened? (plain English, required)" style="width:100%;height:56px"></textarea></div>
<div class="row"><textarea id="fbnotes" placeholder="Notes (optional)" style="width:100%;height:40px"></textarea></div>
<div class="row"><button class="p" onclick="saveFb()">Save feedback</button><button onclick="document.getElementById('fbform').style.display='none'">Cancel</button></div></div>
<div id="orders" style="font-size:12px;margin-top:6px"></div>
<div id="prefs" style="font-size:12px;margin-top:6px"></div>
<div id="next" style="font-size:12px;margin-top:6px"></div></section>
<section><h2>Costs <small>(measured in this database vs assumed scenarios)</small></h2><div id="costs" style="font-size:12px"></div></section>
<section><h2>Open product decisions and evaluation failures</h2><div id="decisions" style="font-size:12px"></div></section>
<section><h2>Audit trail (latest)</h2><div class="ev" id="events"></div></section>
</div></main>
<script>
let sel=null, selPhone=null, selPatient=null;
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(j.error)alert(j.error);refresh();return j}
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function fmtDec(d){if(!d)return '';const keep=['rule','rule_note','reason','intent','confidence','effective_constraints','sites_offered','filter','in_network','due_at','attempt','max_attempts','reduced_cadence','constraints_checked','rejected_site'];const o={};for(const k of keep)if(d[k]!==undefined)o[k]=d[k];return JSON.stringify(o)}
async function refresh(){const s=await (await fetch('/api/state')).json();
document.getElementById('clock').textContent='sim clock '+s.now.replace('T',' ');
const p=document.getElementById('pause');p.textContent=s.paused?('PAUSED: '+s.paused):'outreach running';p.className='tag'+(s.paused?' warn':'');
const f=document.getElementById('feed');f.textContent='partner feed '+(s.feed.latest_generated_at||'none')+(s.feed.stale?' (STALE)':'');f.className='tag'+(s.feed.stale?' warn':'');
document.getElementById('adapters').textContent='model: '+s.adapters.model_id+(s.adapters.model_simulated?' (SIMULATED, not an LLM)':' (LIVE)')+' · sms: '+s.adapters.messaging+(s.adapters.messaging_simulated?' (nothing sent)':(s.adapters.twilio_send_enabled?' (SENDING ENABLED)':' (send disabled)'));
document.getElementById('ver').textContent='v'+s.config.software.ocp_version+' @ '+s.config.software.git_commit;
if(document.activeElement!==document.getElementById('thr'))document.getElementById('thr').value=s.config.overdue_threshold_days;
const m=s.summary;const k=[['Patients / conversations',m.patients+' / '+m.conversations],['Verified complete (partner feed)',m.verified_completions],['Claimed, not verified',m.claimed_not_verified],['Completed outside network (attested)',m.completed_external||0],
['Kate queue open',m.escalations.kate_open],['Clinician queue open (overdue)',m.escalations.clinician_open+' ('+m.escalations.clinician_overdue+')'],['Unresolved orders',(m.orders_by_state.unresolved||0)],['Human min logged / assumed',m.human_minutes_logged+' / '+m.human_minutes_default_assumed],
['Model attempts (simulated)',m.model.attempts_recorded+' (err '+m.model.errors+')'],['SMS segments accepted / in',m.sms.provider_accepted_segments+' / '+m.sms.inbound_segments_original_length],['Conversations with no escalation',m.conversations_with_no_escalation],['Feedback items open',(s.feedback.filter(x=>x.status==='open').length)]];
document.getElementById('kpis').innerHTML=k.map(x=>'<div class="kpi"><b>'+esc(x[1])+'</b><span>'+esc(x[0])+'</span></div>').join('');
document.getElementById('states').textContent='orders: '+JSON.stringify(m.orders_by_state)+' · conversations: '+JSON.stringify(m.conversations_by_state)+' · directory: '+s.directory.verified_sites+' verified sites'+(s.directory.rejected_sites.length?' (rejected: '+s.directory.rejected_sites.join('; ')+')':'');
const qrow=e=>'<tr><td>'+e.id+'</td><td>'+esc(e.display_name)+'</td><td>'+esc(e.label)+'</td><td>'+esc(e.summary)+'</td><td>'+esc((e.due_at||'').replace('T',' '))+(e.overdue?' <b style="color:#a33">OVERDUE</b>':'')+'</td><td>'+esc(e.handoff_status)+(e.after_hours?' (after hours)':'')+'</td><td><button onclick="ack('+e.id+')">accept</button> <button onclick="resolve('+e.id+')">resolve</button></td></tr>';
const hdr='<tr><th>#</th><th>Patient</th><th>Reason</th><th>Summary</th><th>Due</th><th>Handoff (accept = a person took it; nothing is sent)</th><th>Act</th></tr>';
if(s.summary.patients===0)document.getElementById('states').innerHTML='<b style="color:#a33">Empty database.</b> Click "Fresh synthetic session" above (or run python3 -m ocp seed) to get 20 interactive synthetic patients.';
document.getElementById('kq').innerHTML=hdr+(s.kate_queue.length?s.kate_queue.map(qrow).join(''):'<tr><td colspan=7><i>empty</i></td></tr>');
document.getElementById('cq').innerHTML=hdr+(s.clinician_queue.length?s.clinician_queue.map(qrow).join(''):'<tr><td colspan=7><i>empty</i></td></tr>');
document.getElementById('fbt').innerHTML='<tr><th>#</th><th>Patient</th><th>Label</th><th>Flagged message</th><th>Should have happened</th><th>Status</th><th>Act</th></tr>'+(s.feedback.length?s.feedback.map(x=>'<tr><td>'+x.id+'</td><td>'+esc(x.display_name)+'</td><td>'+esc(x.label)+'</td><td>'+esc(x.template_id||'')+': '+esc(x.body)+'…</td><td>'+esc(x.should_have)+'</td><td class="status-'+esc(x.status)+'">'+esc(x.status)+(x.linked_ref?' ('+esc(x.linked_ref)+')':'')+'</td><td><button onclick="exportFb('+x.id+')">export task</button> <button onclick="fbStatus('+x.id+')">status</button></td></tr>').join(''):'<tr><td colspan=7><i>none yet - flag a message in a thread</i></td></tr>');
document.getElementById('convs').innerHTML='<tr><th>#</th><th>Patient</th><th>State</th><th>Orders</th><th>Att.</th><th>Next</th><th>Items</th></tr>'+s.conversations.map(c=>'<tr class="'+(c.id===sel?'sel':'')+'" onclick="pick('+c.id+',\''+esc(c.phone||'')+'\')"><td>'+c.id+'</td><td>'+esc(c.display_name)+(c.synthetic?'':' <b style="color:#a33">NOT SYNTHETIC</b>')+'</td><td>'+esc(c.state)+'</td><td>'+esc(c.order_states)+'</td><td>'+c.outreach_attempts+'</td><td>'+esc(c.next_action||'-')+' '+esc((c.next_action_at||'').replace('T',' '))+'</td><td>'+c.open_items+'</td></tr>').join('');
document.getElementById('events').textContent=s.events.map(e=>e.at.replace('T',' ')+' '+e.actor+' '+e.kind+(e.conversation_id?' c'+e.conversation_id:'')+' '+e.detail).join('\n');
if(sel)loadThread();}
async function pick(id,phone){sel=id;selPhone=phone;refresh()}
async function loadThread(){const t=await (await fetch('/api/conversation/'+sel)).json();if(!t.conversation)return;selPatient=t.conversation.patient_id;
document.getElementById('thread-title').textContent='- '+t.conversation.display_name+' ('+t.conversation.state+')';
document.getElementById('msgs').innerHTML=t.messages.map(m=>'<div class="m '+(m.direction==='inbound'?'in':'out')+'">'+esc(m.body)+'<small>'+(m.direction==='inbound'?'patient · '+m.status:'template '+m.template_id+' · '+m.status+' · '+m.kind)+' · '+m.created_at.replace('T',' ')+(m.direction==='outbound'?' <button class="flag" onclick="openFb('+m.id+',this)">Flag</button>':'')+'</small>'+(m.decision?'<div class="why">why: '+esc(fmtDec(m.decision))+'</div>':'')+'</div>').join('');
document.getElementById('orders').innerHTML='<b>Orders:</b> '+t.orders.map(o=>esc(o.source_order_id)+' ['+esc(o.state)+'] '+esc(o.lines)+(o.verified_at?' · partner-verified '+o.verified_at:'')+(o.claim_location?' · patient-reported at '+esc(o.claim_location)+' (in-network='+esc(o.claim_in_network)+')':'')+(o.intended_due_at?' · intended due '+o.intended_due_at.slice(0,10):'')).join(' · ');
const act=t.preferences.filter(p=>!p.superseded_by);document.getElementById('prefs').innerHTML='<b>Known constraints/preferences:</b> '+(act.length?act.map(p=>esc(p.key)+'='+esc(p.value)+' <i>('+esc(p.source)+(p.corrected?', corrected':'')+')</i>').join(' · '):'none')+' <button onclick="setPref()">set as Kate</button>';
document.getElementById('next').innerHTML='<b>Next action:</b> '+esc(t.conversation.next_action||'none')+' '+esc((t.conversation.next_action_at||'').replace('T',' '))+' - <i>'+esc(t.conversation.next_action_reason||'(no reason recorded)')+'</i>'+(t.escalations.length?'<br><b>Escalations:</b> '+t.escalations.map(e=>'#'+e.id+' '+esc(e.reason)+' → '+esc(e.queue)+' ['+esc(e.status)+', handoff '+esc(e.handoff_status)+']').join(' · '):'');}
let fbMsg=null;
function openFb(mid,btn){fbMsg=mid;document.getElementById('fbmid').textContent=mid;document.getElementById('fbbody').textContent=btn.closest('.m').firstChild.textContent.slice(0,120);document.getElementById('fbform').style.display='block';document.getElementById('fbshould').focus()}
async function saveFb(){const should=document.getElementById('fbshould').value.trim();if(!should)return alert('Say what should have happened.');const r=await post('/api/feedback',{message_id:fbMsg,label:document.getElementById('fblabel').value,should_have:should,notes:document.getElementById('fbnotes').value});if(r.id){document.getElementById('fbform').style.display='none';document.getElementById('fbshould').value='';document.getElementById('fbnotes').value='';alert('Saved feedback #'+r.id+' with captured evidence. Export it from the Feedback table.')}}
async function exportFb(id){const r=await post('/api/feedback/'+id+'/export');if(r.ok)alert('Exported: '+r.path+'\n\nOpen it in VS Code, edit the acceptance criteria if needed, and hand it to Claude/Codex.')}
async function fbStatus(id){const st=prompt('Status: open | linked | verified | dismissed','linked');if(!st)return;const ref=prompt('Linked reference (commit / test / task file), optional','');const note=prompt('Verification note, optional','');await post('/api/feedback/'+id+'/status',{status:st,linked_ref:ref||'',verification_note:note||''})}
async function sendSim(){const b=document.getElementById('reply').value;if(!sel||!selPhone||!b)return alert('Pick a conversation with a phone number first');document.getElementById('reply').value='';await post('/api/sim/inbound',{phone:selPhone,body:b})}
async function linkClick(){if(!sel)return;const r=await post('/api/link_click',{conversation_id:sel});alert('Logged as a signal only. State changed: '+r.state_changed)}
async function ack(id){await post('/api/escalations/'+id+'/acknowledge',{actor:prompt('Who is accepting this item? (kate | clinician)','clinician')||'clinician'})}
async function resolve(id){const nxt=prompt('Next step: resume | close | hold | external_verified (patient completed OUTSIDE the partner network; needs an evidence note)','resume');if(nxt===null)return;const res=prompt(nxt==='external_verified'?'Evidence note (required): who confirmed, where, when':'Resolution note');if(res===null)return;const mins=prompt('Minutes spent (blank = default for this reason, recorded as assumed)');await post('/api/escalations/'+id+'/resolve',{resolution:res,minutes:mins||'',next_step:nxt||'resume'})}
async function setThr(){await post('/api/threshold',{days:parseInt(document.getElementById('thr').value)})}
async function setPref(){if(!selPatient)return;const k=prompt('Preference key (after_time, before_time, weekday, weekend_ok, evening_ok, town, caregiver, reminder_frequency, preferred_site)');if(!k)return;const v=prompt('Value');if(v===null)return;await post('/api/preference',{patient_id:selPatient,key:k,value:v})}
async function loadCosts(){const c=await (await fetch('/api/costs')).json();const mm=c.measured;document.getElementById('costs').innerHTML='<b>Measured (this database, synthetic):</b> outbound/conv '+mm.outbound_per_conversation+', inbound/conv '+mm.inbound_per_conversation+', segments/outbound '+mm.segments_per_outbound+', model attempts/inbound '+mm.model_attempts_per_inbound+', tokens/attempt (mock estimate) '+mm.avg_input_tokens_per_attempt_MOCK_ESTIMATE+', Kate items/conv '+mm.kate_escalations_per_conversation+', clinician items/conv '+mm.clinician_escalations_per_conversation+', unresolved orders '+mm.unresolved_orders+', verified '+mm.verified_completions+', external attested '+mm.completed_external_attested+'<br><b>Assumed scenarios</b> (per patient-month; edit costs/workload_scenarios.json): '+Object.entries(c.assumed_scenarios).map(([n,s])=>n+': out '+s.outbound_msgs+', in '+s.inbound_msgs+', tokens '+s.input_tokens_per_call+'/'+s.output_tokens_per_call+', Kate esc '+s.kate_escalations_per_patient+'@'+s.kate_minutes_per_escalation+'m, clinician esc '+s.clinician_escalations_per_patient+', unresolved '+s.unresolved_rate).join(' · ')+'<br><i>'+esc(c.note)+'</i>'}
async function loadDecisions(){const d=await (await fetch('/api/decisions')).json();document.getElementById('decisions').innerHTML='<b>Decisions for Kate:</b><ol>'+d.decisions.map(x=>'<li><b>'+esc(x.title)+'</b> - '+esc(x.detail)+(x.default?' <i>(current default: '+esc(x.default)+')</i>':'')+'</li>').join('')+'</ol><b>Latest evaluation runs</b> (rule-based simulation unless labeled LIVE):<ul>'+(d.eval_runs.length?d.eval_runs.map(r=>'<li>'+esc(r.label)+' · '+esc(r.set)+': '+r.passed+'/'+r.cases+' pass'+(r.failures.length?'<ul>'+r.failures.map(f=>'<li>'+esc(f.id)+' "'+esc(f.text)+'" - '+esc(f.why)+'</li>').join('')+'</ul>':'')+'</li>').join(''):'<li>none yet - run python3 eval/run_eval.py --adapter mock --set heldout</li>')+'</ul>'}
refresh();loadCosts();loadDecisions();setInterval(refresh,4000);
</script></body></html>"""


_CSS = """
:root{--ink:#1c1f1a;--bg:#f4f5f1;--card:#fff;--line:#dfe3da;--green:#24352a;--warn:#a8541c;--red:#b3261e;--muted:#667}
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
header{background:var(--green);color:#fff;padding:10px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header a{color:#fff;text-decoration:none;opacity:.85}header a.on{opacity:1;font-weight:600;border-bottom:2px solid #fff}
header b{font-size:16px;margin-right:6px}.tag{padding:2px 8px;border-radius:10px;font-size:12px;background:#3c5546}
.tag.warn{background:var(--warn)}.tag.red{background:var(--red)}.tag.sim{background:#5b5b8a}.tag.live{background:#1f6f43}
main{padding:14px;max-width:1280px;margin:0 auto}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:14px}
h2{font-size:12.5px;margin:0 0 8px;color:var(--green);text-transform:uppercase;letter-spacing:.04em}h2 small{text-transform:none;letter-spacing:0;color:var(--muted);font-weight:normal}
table{width:100%;border-collapse:collapse;font-size:12.5px}td,th{padding:4px 6px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600}
tr.click{cursor:pointer}tr.click:hover{background:#eef5ea}.red{color:var(--red);font-weight:600}.muted{color:var(--muted)}
button{font-size:12px;padding:4px 8px;border:1px solid #889;border-radius:5px;background:#fff;cursor:pointer}button.p{background:var(--green);color:#fff;border-color:var(--green)}
input,textarea,select{font-size:12.5px;padding:4px;font-family:inherit}.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.kpi{background:#f1f4ee;border-radius:6px;padding:8px}.kpi b{display:block;font-size:18px}.kpi span{font-size:11px;color:var(--muted)}
.phone{max-width:520px;margin:0 auto;background:#111;border-radius:28px;padding:18px 12px}.screen{background:#fff;border-radius:18px;padding:12px;min-height:520px;max-height:70vh;overflow:auto}
.m{max-width:82%;margin:6px 0;padding:8px 11px;border-radius:14px;font-size:14px;line-height:1.35;white-space:pre-wrap}
.m.out{background:#e9eef7;margin-right:auto;border-bottom-left-radius:4px}.m.in{background:#d9f2d0;margin-left:auto;border-bottom-right-radius:4px}
.m small{display:block;font-size:10.5px;color:var(--muted);margin-top:4px}.m.held{opacity:.6;border:1px dashed #999}
.why{font-size:11px;background:#f6f7f4;border-left:3px solid #c9d1c3;padding:6px 8px;margin:4px 0 8px;max-width:82%;white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace}
.flagbtn{border-color:var(--warn);color:var(--warn);font-size:11px;padding:1px 6px}.whybtn{font-size:11px;padding:1px 6px;color:var(--muted)}
.compose{display:flex;gap:6px;margin-top:8px}.compose input{flex:1;padding:8px;border-radius:16px;border:1px solid #bbb}
.fbform{background:#fff8f2;border:1px solid var(--warn);border-radius:8px;padding:10px;margin:8px 0;font-size:12.5px}
svg text{font-size:10px}
"""

_JS_COMMON = """
function esc(s){return String(s==null?'':s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function post(p,b){const r=await fetch(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(j.error)alert(j.error);return j}
function ts(s){return s?String(s).replace('T',' ').slice(0,16):''}
"""

OPS_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Chief of Health - Operations</title><style>""" + _CSS + r"""</style></head><body>
<header><b>Chief of Health</b><a class="on" href="/">Operations</a><a href="#" id="firstconv">Conversation</a><a href="/referrals">Referrals</a><a href="/facts">Fact cards</a><a href="/improve">Improve</a><span class="tag" id="clock"></span><span class="tag" id="adapters"></span><span class="tag" id="spend"></span><span class="tag" id="pause"></span><span style="margin-left:auto;font-size:12px;opacity:.8" id="ver"></span></header>
<main>
<div class="muted" style="font-size:11.5px;margin:0 0 8px">SYNTHETIC DATA ONLY. Model and writer are SIMULATED unless the header tag says LIVE; SMS never leaves this machine unless the messaging adapter says otherwise.</div>
<section><div class="kpis" id="kpis"></div></section>
<div class="grid">
<section><h2>Conversations active now <small id="activen"></small></h2><table id="active"></table></section>
<section><h2>Escalations open <small>by path and owner; red = past due</small></h2><div id="escs"></div></section>
</div>
<div class="grid">
<section><h2>Partner data connection</h2><div id="conn" style="font-size:12.5px"></div></section>
<section><h2>Patients and orders by state</h2><div class="grid" style="gap:8px"><table id="pstates"></table><table id="ostates"></table></div></section>
</div>
<section><h2>Feed integrity <small>Sept 23: we validate every file on arrival, notice what did not arrive, and tell their technical contact something specific</small></h2><div id="feed" style="font-size:12.5px"></div></section>
<section><h2>Map <small>patients by town (circles), labs they are sent to (squares), mobile phlebotomy route (third layer, empty slot)</small></h2><div id="map"></div></section>
<section><h2>Version 5 <small>referrals, fact cards, the completion funnel, measures</small></h2><div id="v5" style="font-size:12.5px"></div></section>
<section><h2>Automation <small>version 4: who reached a person, and why not</small></h2><div id="auto" style="font-size:12.5px"></div>
<div class="row" style="margin-top:8px;font-size:12px">Clinical handoff: <select id="handoff"><option value="portal_link">portal link (default)</option><option value="portal_relay">portal relay (patient's own words)</option><option value="clinician_queue">our clinician queue (v3)</option></select>
Resolver: <select id="resmode"><option value="on">on</option><option value="shadow">shadow (record only)</option><option value="off">off</option></select>
Quiet days after a referral: <input id="cpause" type="number" min="0" max="30" style="width:48px"><button onclick="saveAuto()">save</button></div></section>
<section><h2>First-text variants <small>the opener is a measured asset: reply, plan and completion rates by variant</small></h2><table id="variants"></table>
<div class="row" style="margin-top:8px;font-size:12px">Disclosure mode for new conversations: <select id="disc"><option value="ab">ab (alternate none/short)</option><option value="none">none</option><option value="short">short clause</option></select>
Sites in opener: <select id="nsites"><option>1</option><option>2</option><option>3</option></select><button onclick="saveSettings()">save</button><span class="muted">partner-negotiated; A/B tested once real patients are on the software</span></div></section>
<div class="grid">
<section><h2>Who wrote the words <small>sent messages by writer; refusals fell back to the approved template</small></h2><div id="composer" style="font-size:12.5px"></div></section>
<section><h2>Controls</h2><div class="row"><button class="p" onclick="fresh()">Fresh synthetic session</button><button onclick="post('/api/tick').then(load)">Run scheduler tick</button><button onclick="adv(24)">+24h</button><button onclick="adv(72)">+72h</button><button onclick="pauseAll()">Pause</button><button onclick="post('/api/resume').then(load)">Resume</button>
Overdue threshold <input id="thr" type="number" min="15" max="45" style="width:56px"><button onclick="post('/api/threshold',{days:parseInt(document.getElementById('thr').value)}).then(load)">set</button>
<a href="/legacy" style="font-size:12px;margin-left:auto">legacy dashboard</a></div><div id="fbnote" class="muted" style="font-size:12px;margin-top:8px"></div></section>
</div>
</main>
<script>""" + _JS_COMMON + r"""
function proj(lat,lon,b){const W=900,H=340,pad=30;const x=pad+(lon-b.minLon)/(b.maxLon-b.minLon||1)*(W-2*pad);const y=pad+(b.maxLat-lat)/(b.maxLat-b.minLat||1)*(H-2*pad);return [x,y]}
function drawMap(m){const pts=[...m.patients.filter(p=>p.lat!=null),...m.sites.filter(s=>s.lat!=null)];if(!pts.length){document.getElementById('map').innerHTML='<i class="muted">no coordinates</i>';return}
const b={minLat:Math.min(...pts.map(p=>p.lat))-0.02,maxLat:Math.max(...pts.map(p=>p.lat))+0.02,minLon:Math.min(...pts.map(p=>p.lon))-0.03,maxLon:Math.max(...pts.map(p=>p.lon))+0.03};
let s='<svg viewBox="0 0 900 340" width="100%" style="background:#eef2ea;border-radius:6px">';
for(const r of (m.mobile_routes||[])){for(const st of (r.stops||[])){const t=(m.towns||{})[st.town];if(!t)continue;const [x,y]=proj(t.lat,t.lon,b);s+='<polygon points="'+x+','+(y-9)+' '+(x+8)+','+(y+6)+' '+(x-8)+','+(y+6)+'" fill="#b5651d" opacity=".85"><title>'+esc(r.name)+' · '+esc(st.town)+' '+esc(st.weekday)+' '+esc((st.window||[]).join('-'))+'</title></polygon><text x="'+(x+10)+'" y="'+(y+12)+'" class="muted">mobile '+esc(st.weekday)+'</text>'}}
for(const st of m.sites){const [x,y]=proj(st.lat,st.lon,b);s+='<rect x="'+(x-7)+'" y="'+(y-7)+'" width="14" height="14" fill="'+(st.valid?'#24352a':'#bbb')+'" rx="2"><title>'+esc(st.name)+' · '+esc((st.services||[]).join(', '))+(st.valid?'':' · NOT VALID')+'</title></rect><text x="'+(x+10)+'" y="'+(y+4)+'">'+esc(st.name.replace('Riverbend ',''))+(st.sent_to?' ('+st.sent_to+' planned)':'')+'</text>'}
for(const p of m.patients){if(p.lat==null)continue;const [x,y]=proj(p.lat,p.lon,b);const r=6+Math.sqrt(p.n)*4;s+='<circle cx="'+x+'" cy="'+y+'" r="'+r+'" fill="rgba(31,111,67,.35)" stroke="#1f6f43"><title>'+esc(p.town)+': '+p.n+' patients, '+p.active+' active</title></circle><text x="'+(x-r)+'" y="'+(y-r-3)+'">'+esc(p.town)+' '+p.n+'</text>'}
s+='<text x="12" y="330" class="muted">Layers: patients (green circles) · labs (dark squares) · mobile route stops (orange triangles): '+((m.mobile_routes||[]).length?(m.mobile_routes.map(r=>r.name).join(', ')):'none configured')+'</text></svg>';document.getElementById('map').innerHTML=s}
async function load(){const o=await (await fetch('/api/ops')).json();
document.getElementById('clock').textContent='sim clock '+ts(o.now);
const a=document.getElementById('adapters');a.textContent='model '+o.adapters.model_id+(o.adapters.model_simulated?' (simulated)':' (LIVE)')+' · writer '+(o.adapters.composer_model||o.adapters.composer)+(o.adapters.composer_simulated?' (deterministic)':' (LIVE)')+' · sms '+o.adapters.messaging;a.className='tag '+(o.adapters.model_simulated&&o.adapters.composer_simulated?'sim':'live');
const sp=document.getElementById('spend');sp.textContent='spend 24h $'+o.spend.last_24h_usd.toFixed(2)+' / cap $'+o.spend.cap_usd_per_day.toFixed(2);sp.className='tag'+(o.spend.cap_reached?' red':'');
const p=document.getElementById('pause');p.textContent=o.paused?('PAUSED: '+o.paused):'running';p.className='tag'+(o.paused?' warn':'');
document.getElementById('ver').textContent='v'+o.software.ocp_version+' @ '+o.software.git_commit;
if(document.activeElement!==document.getElementById('thr'))document.getElementById('thr').value=o.settings.overdue_threshold_days;
if(document.activeElement!==document.getElementById('disc'))document.getElementById('disc').value=o.settings.opener_disclosure;
if(document.activeElement!==document.getElementById('nsites'))document.getElementById('nsites').value=o.settings.opener_sites;
if(document.activeElement!==document.getElementById('handoff'))document.getElementById('handoff').value=o.settings.clinical_handoff;
if(document.activeElement!==document.getElementById('resmode'))document.getElementById('resmode').value=o.settings.resolver_mode;
if(document.activeElement!==document.getElementById('cpause'))document.getElementById('cpause').value=o.settings.clinical_pause_days;
const v5=o.v5;const fu=v5.funnel;const me=v5.measures;document.getElementById('v5').innerHTML='<table><tr><th>Referrals</th><td>'+v5.referrals_total+' total · overdue '+v5.referrals.overdue+' · duplicates '+v5.referrals.duplicates+' · failed '+v5.referrals.failed+' · ambiguous routing '+v5.referrals.ambiguous_routing+' · missing rationale '+v5.referrals.missing_rationale+' · <a href="/referrals">open the dashboard</a></td></tr>'+
'<tr><th>Fact cards</th><td>'+esc(JSON.stringify(v5.facts))+' · role now: <b>'+esc(v5.operator_role)+'</b> (simulated switch) · <a href="/facts">chart review</a></td></tr>'+
'<tr><th>Completion funnel</th><td>walk-in plans now '+fu.walk_in_plans_now+' · ever planned '+fu.conversations_ever_planned+' · <b>confirmed bookings</b> '+fu.conversations_with_a_confirmed_booking+' ('+esc(v5.scheduler.adapter)+(v5.scheduler.simulated?', SIMULATED':'')+') · attended '+fu.conversations_attended+' · <b>verified complete</b> '+fu.patients_verified_complete+' · booking failures '+fu.bookings_failed+'</td></tr>'+
'<tr><th>Measures</th><td>reply rate '+(me.experience.reply_rate_pct==null?'-':me.experience.reply_rate_pct+'%')+' · flags/100 '+me.experience.flags_per_100_conversations+' · clinician items/100 '+me.escalation.clinician_facing_items_per_100+' · labeled: missed '+me.escalation.labeled_cases.missed+' / unnecessary '+me.escalation.labeled_cases.unnecessary+' of '+me.escalation.labeled_cases.cases+' · human min '+me.staff_workload.human_minutes_logged+' · chart-review min '+me.staff_workload.chart_review_minutes+'</td></tr></table><div class="muted" style="margin-top:6px">'+esc(me.principle)+'</div>';
const au=o.automation;document.getElementById('auto').innerHTML='<table><tr><th>Clinician-facing items / 100 conversations</th><td><b>'+au.clinician_items_per_100_conversations+'</b></td><th>Kate items / 100</th><td><b>'+au.kate_items_per_100_conversations+'</b></td></tr>'+
'<tr><th>Portal referrals</th><td>'+au.portal_referrals+'</td><th>Prep questions answered from approved text</th><td>'+au.prep_answered_from_approved_text+'</td></tr>'+
'<tr><th>Portal relays sent (patient\'s words)</th><td>'+au.portal_relays_sent+'</td><th>Emergency texts</th><td>'+au.emergency_texts+'</td></tr>'+
'<tr><th>Resolver ('+esc(au.resolver.mode)+', reviewer: '+esc(au.resolver.reviewer_label)+')</th><td>tried '+au.resolver.attempted+' · solved '+au.resolver.executed+' · disagreements '+au.resolver.disagreements+' · failed '+au.resolver.failed+'</td><th>Menu digits decided by code</th><td>'+au.menu_digits_decided+'</td></tr>'+
'<tr><th>Planner shadow (model vs rules)</th><td>'+au.planner_shadow.compared+' compared · '+(au.planner_shadow.agreement_pct==null?'-':au.planner_shadow.agreement_pct+'% agree')+'</td><th>Locations recorded</th><td>'+au.locations_recorded+'</td></tr></table>'+
'<div class="muted" style="margin-top:6px">'+esc(au.note)+' · clinical route: <b>'+esc(au.clinical_route)+'</b>'+(au.portal?' ('+esc(au.portal.name)+')':'')+(au.clinical_route==='none'?' — <b>no portal link and the partner has not opted into a clinician queue: patients get the clinic phone only</b>':'')+'</div>';
const ob=o.orders_by_state;document.getElementById('kpis').innerHTML=[['Patients',o.patients],['Active conversations',o.active.length],['Open items (Kate / clinician)',(o.escalations.filter(e=>e.queue==='kate').length)+' / '+(o.escalations.filter(e=>e.queue==='clinician').length)],['Verified complete',(ob.verified_complete||0)],['Claimed, not verified',(ob.claimed_complete||0)],['Unresolved',(ob.unresolved||0)],['Feedback open',o.feedback_open],['Composer refusals',o.composer.refusals]].map(x=>'<div class="kpi"><b>'+esc(x[1])+'</b><span>'+esc(x[0])+'</span></div>').join('');
document.getElementById('activen').textContent=o.active.length+' · '+Object.entries(o.active_by_state).map(([k,v])=>k+' '+v).join(', ');
document.getElementById('active').innerHTML='<tr><th>Patient</th><th>Town</th><th>State</th><th>Last message</th><th>Next</th></tr>'+(o.active.length?o.active.map(c=>'<tr class="click" onclick="location.href=\'/conversation/'+c.id+'\'"><td>'+esc(c.display_name)+'</td><td>'+esc(c.home_town||'')+'</td><td>'+esc(c.state)+'</td><td>'+(c.last_dir==='inbound'?'<b>patient:</b> ':'')+esc((c.last_body||'').slice(0,90))+' <span class="muted">'+ts(c.last_at)+'</span></td><td class="muted">'+esc(c.next_action||'-')+' '+ts(c.next_action_at)+'</td></tr>').join(''):'<tr><td colspan=5><i>none</i></td></tr>');
if(o.active.length)document.getElementById('firstconv').href='/conversation/'+o.active[0].id;
let eh='';for(const [path,owners] of Object.entries(o.escalations_by_path)){eh+='<h3 style="font-size:12px;margin:8px 0 4px">'+esc(path)+' path</h3>';for(const [owner,items] of Object.entries(owners)){eh+='<div class="muted" style="font-size:11.5px">owner: '+esc(owner)+' · '+items.length+'</div><table>'+items.map(e=>'<tr class="click" onclick="location.href=\'/conversation/'+e.conversation_id+'\'"><td>'+esc(e.display_name)+'</td><td>'+esc(e.label)+'</td><td class="'+(e.overdue||e.due_passed?'red':'muted')+'">due '+ts(e.due_at)+'</td><td class="muted">'+esc(e.handoff_status)+'</td></tr>').join('')+'</table>'}}
document.getElementById('escs').innerHTML=eh||'<i class="muted">no open escalations</i>';
const c=o.connection;document.getElementById('conn').innerHTML='<div class="row"><span class="tag '+(c.late?'red':'live')+'">'+(c.late?'LATE':'on time')+'</span><b>'+esc(c.kind)+'</b></div><table><tr><td>Last file generated</td><td>'+ts(c.last_received)+'</td></tr><tr><td>Last imported</td><td>'+ts(c.last_imported)+'</td></tr><tr><td>Next pickup expected</td><td>'+ts(c.next_pickup)+'</td></tr><tr><td>Stale after</td><td>'+c.stale_after_hours+' h</td></tr><tr><td>Imports so far</td><td>'+c.imports+'</td></tr></table><div class="muted" style="margin-top:6px">'+esc(c.note)+'</div>';
document.getElementById('pstates').innerHTML='<tr><th>Conversations</th><th></th></tr>'+Object.entries(o.patients_by_state).map(([k,v])=>'<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
document.getElementById('ostates').innerHTML='<tr><th>Orders</th><th></th></tr>'+Object.entries(o.orders_by_state).map(([k,v])=>'<tr><td>'+esc(k)+'</td><td>'+v+'</td></tr>').join('');
drawMap(o.map);loadFeed();
const V=Object.entries(o.variants);document.getElementById('variants').innerHTML='<tr><th>Variant</th><th>Sent</th><th>Replied</th><th>Reply rate</th><th>Median h to first reply</th><th>Plan rate</th><th>Completion rate</th></tr>'+(V.length?V.map(([k,g])=>'<tr><td>'+esc(k)+'</td><td>'+g.sent+'</td><td>'+g.replied+'</td><td>'+(g.reply_rate_pct==null?'-':g.reply_rate_pct+'%')+'</td><td>'+(g.median_hours_to_first_reply==null?'-':g.median_hours_to_first_reply)+'</td><td>'+(g.plan_rate_pct==null?'-':g.plan_rate_pct+'%')+'</td><td>'+(g.completion_rate_pct==null?'-':g.completion_rate_pct+'%')+'</td></tr>').join(''):'<tr><td colspan=7><i>no openers sent yet</i></td></tr>');
const w=o.composer.sent_by_writer;document.getElementById('composer').innerHTML='<table><tr><th>Writer</th><th>Sent</th></tr><tr><td>Claude (live)</td><td>'+w.anthropic+'</td></tr><tr><td>deterministic fact writer</td><td>'+w.fact+'</td></tr><tr><td>approved template (fallback / compliance / safety)</td><td>'+w.template+'</td></tr></table><div class="muted" style="margin-top:6px">fact-check refusals: '+o.composer.refusals+' · live spend by purpose: '+esc(JSON.stringify(o.spend.by_purpose))+'</div>';
document.getElementById('fbnote').textContent='Flag any reply from its conversation page; flags are the examples the writer is tuned against.';}
async function loadFeed(){const f=await (await fetch('/api/feed')).json();let h='<div class="row"><span class="tag '+(f.blocked?'red':'live')+'">'+(f.blocked?'BLOCKED: '+esc(f.blocked):'feed ok')+'</span><span class="muted">pickup: '+esc(f.source.adapter)+(f.source.path?' ('+esc(f.source.path)+')':'')+' · notifier: '+esc(f.notifier.adapter)+(f.notifier.simulated?' (simulated)':'')+' · partner technical contact: '+esc(f.technical_contact.name||'none on the directory')+'</span></div>';
h+='<table><tr><th>Kind</th><th>Status</th><th>Last accepted (generated)</th><th>Files ok / held</th><th>Usual rows</th><th>Last results event</th></tr>'+(f.health.length?f.health.map(x=>'<tr><td>'+esc(x.feed_kind)+'</td><td class="'+(x.status==='blocked'?'red':'')+'">'+esc(x.status)+(x.blocked_reason?' — '+esc(x.blocked_reason):'')+(x.status==='blocked'?' <button onclick="unblockFeed(\''+esc(x.feed_kind)+'\')">clear with a note</button>':'')+'</td><td>'+ts(x.last_generated_at)+'</td><td>'+x.accepted_files+' / '+x.rejected_files+'</td><td>'+(x.baseline_count==null?'-':Math.round(x.baseline_count))+'</td><td>'+ts(x.last_result_event_at)+'</td></tr>').join(''):'<tr><td colspan=6><i>no files yet</i></td></tr>')+'</table>';
h+='<h3 style="font-size:12px;margin:8px 0 4px">Open alerts '+f.open_alerts.length+'</h3>'+(f.open_alerts.length?f.open_alerts.map(a=>'<details style="margin:4px 0"><summary><span class="tag '+(a.severity==='blocking'?'red':(a.severity==='warning'?'warn':''))+'">'+esc(a.severity)+'</span> <b>'+esc(a.subject)+'</b> <span class="muted">'+esc(a.code)+' · seen '+a.occurrences+'× · '+esc(a.status)+(a.notified_at?' · partner notified '+ts(a.notified_at)+' ('+esc(a.notify_channel)+')':(a.partner_action_needed?' · NOT notified':' · operator only'))+'</span></summary><div class="muted">'+esc(a.message_internal)+'</div>'+(a.message_to_partner?'<pre style="white-space:pre-wrap;font-size:11.5px;background:rgba(127,127,127,.08);padding:6px">'+esc(a.message_to_partner)+'</pre>':'')+'<div class="row">'+(a.status==='open'?'<button onclick="ackFeed('+a.id+')">acknowledge</button>':'')+'<button onclick="resolveFeed('+a.id+')">resolve with a note</button></div></details>').join(''):'<i class="muted">none</i>');
h+='<div class="muted" style="margin-top:6px">thresholds: '+Object.entries(f.policy).map(([k,v])=>esc(k.replace('feed_',''))+'='+v).join(' · ')+' · '+esc(f.source.note)+' · holds: '+f.holds.length+'</div>';
h+='<h3 style="font-size:12px;margin:8px 0 4px">Recent files</h3><table><tr><th>Received</th><th>Kind</th><th>Source</th><th>Rows</th><th>Held</th><th>Verdict</th><th>Checks</th></tr>'+(f.receipts.length?f.receipts.map(r=>'<tr><td>'+ts(r.received_at)+'</td><td>'+esc(r.feed_kind||'?')+'</td><td>'+esc(r.source)+(r.source_name?' '+esc(r.source_name):'')+'</td><td>'+(r.record_count==null?'-':r.record_count)+'</td><td>'+r.quarantined_count+'</td><td class="'+(r.verdict==='rejected'?'red':'')+'">'+esc(r.verdict)+(r.has_payload?' <button onclick="reprocessFeed('+r.id+')">reprocess</button>':'')+'</td><td class="muted">'+r.checks.filter(c=>c.status!=='pass').map(c=>esc(c.check)+': '+esc(c.detail)).join(' · ')+'</td></tr>').join(''):'<tr><td colspan=7><i>none</i></td></tr>')+'</table>';
document.getElementById('feed').innerHTML=h}
function who(){let a=localStorage.getItem('operator_name')||'';if(!a){a=prompt('Your name (recorded on the audit trail)')||'';if(a)localStorage.setItem('operator_name',a)}return a}
async function ackFeed(id){const a=who();if(!a)return;const n=prompt('Note (optional)')||'';await post('/api/feed/alerts/'+id+'/acknowledge',{actor:a,note:n});loadFeed()}
async function resolveFeed(id){const a=who();if(!a)return;const n=prompt('Resolution note');if(n){await post('/api/feed/alerts/'+id+'/resolve',{actor:a,resolution:n});loadFeed()}}
async function unblockFeed(kind){const a=who();if(!a)return;const n=prompt('Why is it safe to clear the '+kind+' block without a new file?');if(n){await post('/api/feed/unblock',{kind:kind,actor:a,note:n});load()}}
async function reprocessFeed(id){const a=who();if(!a)return;if(confirm('Re-run receipt '+id+' from its retained payload?')){await post('/api/feed/receipts/'+id+'/reprocess',{actor:a});load()}}
async function adv(h){await post('/api/advance',{hours:h});await post('/api/tick');load()}
async function pauseAll(){const r=prompt('Reason for pausing all outreach','manual');if(r!==null){await post('/api/pause',{reason:r});load()}}
async function fresh(){if(confirm('Reset the synthetic cohort? Feedback is kept and archived.')){await post('/api/session/fresh');load()}}
async function saveAuto(){await post('/api/settings',{clinical_handoff:document.getElementById('handoff').value,resolver_mode:document.getElementById('resmode').value,clinical_pause_days:parseInt(document.getElementById('cpause').value)});load()}
async function saveSettings(){await post('/api/settings',{opener_disclosure:document.getElementById('disc').value,opener_sites:parseInt(document.getElementById('nsites').value)});load()}
load();setInterval(load,5000);
</script></body></html>"""

_NAV = r"""<header><b>Chief of Health</b><a href="/">Operations</a><a href="/referrals" id="n_ref">Referrals</a><a href="/facts" id="n_facts">Fact cards</a><a href="/improve" id="n_imp">Improve</a><span class="tag" id="role"></span><span style="margin-left:auto;font-size:11.5px;opacity:.8">SYNTHETIC · operational oversight, not a clinician inbox</span></header>"""

REFERRALS_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Chief of Health - Referrals</title><style>""" + _CSS + r"""</style></head><body>""" + _NAV + r"""
<main>
<section><h2>Clinical referrals <small>every handoff: portal-link-only, relayed, queued, emergency, route missing, plan change. "Sent" is not "received"; "received" is not "resolved".</small></h2>
<div class="row" style="font-size:12px;margin-bottom:6px">reason <select id="f_reason"><option value="">any</option><option>clinical_question</option><option>staff_request</option><option>emergency</option><option>plan_change_reported</option></select>
state <select id="f_state"><option value="">any</option><option>offered</option><option>awaiting_patient</option><option>sent</option><option>queued</option><option>acknowledged</option><option>responded</option><option>resolved</option><option>failed</option><option>expired</option><option>cancelled</option></select>
team <select id="f_team"><option value="">any</option><option>ordering_clinician</option><option>pcp</option><option>episode_team</option><option>nurse_line</option></select>
missing <select id="f_missing"><option value="">any</option><option value="order_rationale">rationale</option><option value="care_team">care team</option></select>
<label><input type="checkbox" id="f_overdue"> overdue only</label> min age h <input id="f_age" type="number" style="width:50px"><button onclick="load()">filter</button></div>
<div id="summary" class="muted" style="font-size:12px;margin-bottom:6px"></div><table id="list"></table></section>
<section id="detail" style="display:none"><h2>Referral <span id="d_id"></span></h2><div id="d_body" style="font-size:12.5px"></div></section>
<section><h2>Policy shown for review <small>drafts until the partner approves them</small></h2><div id="policy" style="font-size:12px"></div></section>
</main>
<script>""" + _JS_COMMON + r"""
const sel=parseInt(location.pathname.split('/')[2]||'0')||null;
function go(id){location.href='/referrals/'+id}
function q(){const p=new URLSearchParams();for(const [k,id] of [['reason','f_reason'],['state','f_state'],['team','f_team'],['missing','f_missing']]){const v=document.getElementById(id).value;if(v)p.set(k,v)}if(document.getElementById('f_overdue').checked)p.set('overdue','1');const a=document.getElementById('f_age').value;if(a)p.set('min_age_hours',a);return p.toString()}
async function load(){const o=await (await fetch('/api/referrals?'+q())).json();document.getElementById('role').textContent='';
document.getElementById('summary').textContent=o.referrals.length+' shown · overdue '+o.overdue+' · duplicates '+o.duplicates+' · failed '+o.failed+' · ambiguous routing '+o.ambiguous_routing+' · missing rationale '+o.missing_rationale+' · by state '+JSON.stringify(o.by_state)+' · by team '+JSON.stringify(o.by_team);
document.getElementById('list').innerHTML='<tr><th>#</th><th>Patient</th><th>Kind / reason</th><th>Urgency</th><th>Team (basis)</th><th>State · delivery</th><th>Owner · next</th><th>Age</th><th>Flags</th></tr>'+(o.referrals.length?o.referrals.map(r=>'<tr class="click'+(r.id===sel?' sel':'')+'" onclick="go('+r.id+')"><td>'+r.id+'</td><td>'+esc(r.display_name)+'</td><td>'+esc(r.kind)+' / '+esc(r.reason)+(r.topic?' ('+esc(r.topic)+')':'')+'</td><td>'+esc(r.urgency)+(r.urgency_policy_status!=='approved'?' <span class="muted">(policy pending)</span>':'')+'</td><td>'+esc(r.receiving_team)+' · '+esc(r.receiving_name||'')+(r.routing_ambiguous?' <b class="red">AMBIGUOUS</b>':'')+'</td><td>'+esc(r.state)+' · '+esc(r.delivery_label)+'</td><td>'+esc(r.owner||'')+' · '+esc(r.next_action||'')+(r.response_due_at?' · due '+ts(r.response_due_at):'')+'</td><td class="'+(r.overdue?'red':'')+'">'+r.age_hours+'h'+(r.overdue?' OVERDUE':'')+'</td><td>'+(r.duplicate_of?'dup of #'+r.duplicate_of+' ':'')+(r.missing_data.length?'missing: '+esc(r.missing_data.join(', ')):'')+'</td></tr>').join(''):'<tr><td colspan=9><i>none</i></td></tr>');
document.getElementById('policy').innerHTML='<b>Routes</b> ('+(o.routes.approved?'partner-approved':'DRAFT — pending partner approval')+'): '+esc(JSON.stringify(o.routes.routes))+'<br><b>Urgency policy:</b> '+esc(o.urgency_policy.status)+' '+esc(JSON.stringify(o.urgency_policy.levels||{}))+'<br><b>Escalation criteria:</b> '+esc(o.escalation_criteria.status)+' — '+esc((o.escalation_criteria.criteria||[]).join(' · '))+'<br><i>'+esc(o.note)+'</i>';
if(sel)detail()}
async function detail(){const d=await (await fetch('/api/referrals/'+sel)).json();if(!d.referral)return;const r=d.referral;document.getElementById('detail').style.display='block';document.getElementById('d_id').textContent='#'+r.id+' · '+r.display_name+' · '+r.kind+' / '+r.reason;
let h='<table><tr><th>State · delivery</th><td><b>'+esc(r.state)+'</b> · '+esc(r.delivery)+(r.kind==='portal_link'?' — <b>delivery to the provider is unverified</b> unless evidence arrives':'')+'</td></tr>'+
'<tr><th>Patient\'s own words</th><td>'+esc(r.patient_words||'(scheduled)')+'</td></tr><tr><th>Generated summary <span class="muted">(rule-built, labeled)</span></th><td><i>'+esc(r.generated_summary||'')+'</i></td></tr>'+
'<tr><th>Intended receiving team</th><td>'+esc(r.receiving_team)+' · '+esc(r.receiving_name||'')+'<br><span class="muted">'+esc(r.routing_basis||'')+'</span>'+(r.routing_ambiguous?'<br><b class="red">routing ambiguous — care-team record incomplete or conflicting</b>':'')+'</td></tr>'+
'<tr><th>Urgency</th><td>'+esc(r.urgency)+' · policy '+esc(r.urgency_policy_status)+'</td></tr><tr><th>Created · owner · next · window</th><td>'+ts(r.created_at)+' · '+esc(r.owner||'')+' · '+esc(r.next_action||'')+' · due '+ts(r.response_due_at)+(r.overdue?' <b class="red">OVERDUE</b>':'')+'</td></tr>'+
'<tr><th>Source facts</th><td>documented: '+(d.facts.documented.length?d.facts.documented.map(f=>'"'+esc(f.excerpt)+'" — '+esc(f.author)+' (card '+f.card_id+')').join('; '):'<b>none</b>')+'<br>unresolved: '+(d.facts.unresolved.length?d.facts.unresolved.map(f=>'card '+f.card_id+': '+esc(f.reason)).join('; '):'none')+'</td></tr>'+
'<tr><th>Orders</th><td>'+d.orders.map(o=>esc(o.source_order_id)+' ['+esc(o.state)+'] '+esc(o.lines||'')).join('<br>')+'</td></tr>'+
(d.portal_message?'<tr><th>Portal message (patient\'s words)</th><td>'+esc(d.portal_message.body)+' · '+esc(d.portal_message.status)+' · '+esc(d.portal_message.adapter)+'</td></tr>':'')+
(d.escalation?'<tr><th>Linked queue item</th><td>#'+d.escalation.id+' '+esc(d.escalation.reason)+' · '+esc(d.escalation.status)+' · handoff '+esc(d.escalation.handoff_status)+'</td></tr>':'')+
(r.resolved_at?'<tr><th>Resolved</th><td>'+ts(r.resolved_at)+' by '+esc(r.resolved_by)+' · basis: '+esc(r.resolution_basis)+' · authority: '+esc(r.resolution_authority)+' · outcome '+esc(r.outcome)+'</td></tr>':'')+'</table>';
h+='<h3 style="font-size:12px;margin:8px 0 4px">Audit trail</h3><div class="ev">'+d.events.map(e=>ts(e.at)+' · '+esc(e.actor)+' · <b>'+esc(e.kind)+'</b> '+esc(JSON.stringify(e.detail))).join('<br>')+'</div>';
h+='<h3 style="font-size:12px;margin:8px 0 4px">Conversation</h3><div class="ev">'+d.thread.map(m=>(m.direction==='inbound'?'<b>patient:</b> ':'app ('+esc(m.template_id)+'): ')+esc(m.body)).join('<br>')+'</div>';
h+='<div class="row" style="margin-top:8px"><input id="ev_kind" placeholder="partner evidence: delivered | acknowledged | responded | failed" style="width:300px"><button onclick="evidence()">record partner evidence (entered by hand)</button></div>';
h+='<div class="row" style="margin-top:6px"><input id="basis" placeholder="resolution basis (required)" style="width:320px"><select id="rrole"><option value="operator">as operator</option><option value="clinical_reviewer">as clinical reviewer (simulated)</option></select><button onclick="resolve()">resolve</button><span class="muted">'+(d.can_operator_resolve?'operator may resolve':'clinical referral: needs clinical authority or partner response evidence')+'</span></div>';
h+='<div class="row" style="margin-top:6px"><input id="fb" placeholder="flag: what should have happened?" style="width:320px"><select id="fbcat"><option>routing</option><option>tone</option><option>factual_support</option><option>operational_effectiveness</option><option>longitudinal_order_handling</option></select><button onclick="flag()">flag for improvement</button></div>';
document.getElementById('d_body').innerHTML=h}
async function evidence(){const k=document.getElementById('ev_kind').value.trim();const r=await post('/api/referrals/'+sel+'/evidence',{kind:k});if(!r.ok)alert(r.reason);load()}
async function resolve(){const r=await post('/api/referrals/'+sel+'/resolve',{basis:document.getElementById('basis').value,role:document.getElementById('rrole').value,authority:'dashboard'});if(!r.ok)alert(r.reason);load()}
async function flag(){const r=await post('/api/referrals/'+sel+'/flag',{should_have:document.getElementById('fb').value,category:document.getElementById('fbcat').value});if(r.id)alert('flagged as feedback #'+r.id);load()}
load();
</script></body></html>"""

FACTS_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Chief of Health - Fact cards</title><style>""" + _CSS + r"""</style></head><body>""" + _NAV + r"""
<main>
<section><h2>Fact cards and chart review <small>quotation, never interpretation; approval authority follows the card's class</small></h2>
<div class="row" style="font-size:12px;margin-bottom:6px">status <select id="f_status"><option value="">any</option><option>proposed</option><option>approved</option><option>flagged</option><option>rejected</option><option>superseded</option></select>
kind <select id="f_kind"><option value="">any</option><option>order_rationale</option><option>prep_instruction</option><option>care_team</option><option>contact_preference</option><option>general_education</option><option>order_status_note</option></select>
<button onclick="load()">filter</button> · role: <select id="rolesel"><option value="operator">operator (Kate)</option><option value="clinical_reviewer">clinical reviewer (SIMULATED)</option></select><button onclick="setRole()">switch</button><span class="muted">the role switch is a demo stand-in for the partner's identity system</span></div>
<div id="notes" class="muted" style="font-size:12px;margin-bottom:6px"></div><table id="list"></table></section>
<section id="detail" style="display:none"><h2>Card <span id="d_id"></span></h2><div class="grid"><div id="d_source" style="font-size:12.5px"></div><div id="d_card" style="font-size:12.5px"></div></div></section>
</main>
<script>""" + _JS_COMMON + r"""
const sel=parseInt(location.pathname.split('/')[2]||'0')||null;
function go(id){location.href='/facts/'+id}
async function load(){const p=new URLSearchParams();const st=document.getElementById('f_status').value;if(st)p.set('status',st);const k=document.getElementById('f_kind').value;if(k)p.set('kind',k);
const o=await (await fetch('/api/facts?'+p)).json();document.getElementById('role').textContent='role: '+o.role;document.getElementById('rolesel').value=o.role;
document.getElementById('notes').innerHTML='Notes on file (synthetic): '+o.notes.map(n=>esc(n.note_id)+' '+esc(n.author)+' '+ts(n.authored_at)+' ['+esc(n.access_basis)+'] '+esc(n.display_name)+' <button onclick="extract('+n.patient_id+')">extract</button>').join(' · ');
document.getElementById('list').innerHTML='<tr><th>#</th><th>Patient · order</th><th>Kind · class</th><th>Statement</th><th>Source · method</th><th>Status · review</th></tr>'+(o.cards.length?o.cards.map(c=>'<tr class="click'+(c.id===sel?' sel':'')+'" onclick="go('+c.id+')"><td>'+c.id+' v'+c.version+'</td><td>'+esc(c.display_name)+' · '+esc(c.source_order_id||'')+' '+esc(c.tests||'')+'</td><td>'+esc(c.kind)+' · <b>'+esc(c['class'])+'</b>'+(c.requires_clinical_review?' <span class="muted">(clinical reviewer)</span>':'')+'</td><td>'+esc(c.statement)+'</td><td>'+esc(c.source_ref||'—')+' · '+esc(c.extraction_method)+'</td><td>'+esc(c.status)+(c.reviewed_by?' by '+esc(c.reviewed_by)+' ('+esc(c.reviewer_role)+')':'')+(c.conflict_group?' <b class="red">CONFLICT</b>':'')+'</td></tr>').join(''):'<tr><td colspan=6><i>none — import notes and extract</i></td></tr>');
if(sel)detail()}
async function detail(){const d=await (await fetch('/api/facts/'+sel)).json();if(!d.card)return;const c=d.card;document.getElementById('detail').style.display='block';document.getElementById('d_id').textContent='#'+c.id+' v'+c.version+' · '+c.display_name+' · '+c.kind;
const src=d.source;document.getElementById('d_source').innerHTML='<h3 style="font-size:12px">Source</h3>'+(src?'<div class="muted">'+esc(src.note_id)+' · '+esc(src.author)+' ('+esc(src.author_role||'')+') · '+ts(src.authored_at)+' · access: '+esc(src.access_basis)+'</div><div style="white-space:pre-wrap;border:1px solid #ddd;padding:8px;border-radius:6px;margin-top:4px">'+(c.excerpt?esc(src.text).replace(esc(c.excerpt),'<mark>'+esc(c.excerpt)+'</mark>'):esc(src.text))+'</div>':'<i>no source note (gap card)</i>');
let h='<h3 style="font-size:12px">Proposed fact</h3><table><tr><th>Class</th><td><b>'+esc(c['class'])+'</b></td></tr><tr><th>Statement</th><td>'+esc(c.statement)+'</td></tr><tr><th>Excerpt (verbatim)</th><td>'+esc(c.excerpt||'—')+'</td></tr><tr><th>Author · date</th><td>'+esc(c.author||'—')+' · '+ts(c.authored_at)+'</td></tr><tr><th>Method · proposed</th><td>'+esc(c.extraction_method)+' · '+esc(c.proposed_by)+' '+ts(c.proposed_at)+'</td></tr><tr><th>Status</th><td>'+esc(c.status)+(c.verified_at?' · verified '+ts(c.verified_at)+' by '+esc(c.reviewed_by)+' ('+esc(c.reviewer_role)+')':'')+(c.flag_reason?' · <i>'+esc(c.flag_reason)+'</i>':'')+'</td></tr>'+
(d.conflicts.length?'<tr><th class="red">Conflicts with</th><td>'+d.conflicts.map(x=>'#'+x.id+' "'+esc(x.statement)+'" ('+esc(x.source_ref)+', '+esc(x.author)+', '+esc(x.status)+')').join('<br>')+'</td></tr>':'')+
(d.supersedes_chain.length?'<tr><th>Supersedes</th><td>'+d.supersedes_chain.map(x=>'#'+x.id+' v'+x.version+' "'+esc(x.statement)+'" ('+esc(x.status)+')').join('<br>')+'</td></tr>':'')+'</table>';
h+='<div class="muted" style="margin:6px 0">'+esc(d.approval.note)+' · this card '+(d.approval.requires_clinical_review?'<b>requires the clinical reviewer</b>':'may be approved by the operator')+'</div>';
h+='<div class="row"><textarea id="stmt" style="width:100%;height:50px" placeholder="edit: for a rationale card, paste the exact sentence from the source (verbatim); anything else is refused"></textarea></div>';
h+='<div class="row" style="margin-top:6px"><input id="note" placeholder="review note" style="width:260px"><input id="mins" type="number" step="0.5" placeholder="minutes" style="width:70px"><button class="p" onclick="act(&quot;approve&quot;)">approve</button><button onclick="act(&quot;edit&quot;)">save edit (new version)</button><button onclick="act(&quot;flag&quot;)">flag for partner clinical clarification</button><button onclick="act(&quot;reject&quot;)">reject</button></div>';
h+='<div class="row" style="margin-top:6px"><input id="fb" placeholder="flag this card for improvement: what should have happened?" style="width:320px"><button onclick="flag()">flag</button></div><div style="margin-top:8px"><b>History</b><div class="ev">'+d.history.map(x=>ts(x.at)+' · '+esc(x.actor)+' ('+esc(x.role)+') '+esc(x.action)+' '+esc(x.from_status||'')+'→'+esc(x.to_status||'')+(x.note?' · '+esc(x.note):'')+(x.minutes?' · '+x.minutes+' min':'')).join('<br>')+'</div></div>';
document.getElementById('d_card').innerHTML=h}
async function act(a){const r=await post('/api/facts/'+sel+'/review',{action:a,statement:document.getElementById('stmt').value,note:document.getElementById('note').value,minutes:document.getElementById('mins').value});if(r.ok===false)alert('refused: '+r.reason);if(r.card_id&&r.card_id!==sel)location.href='/facts/'+r.card_id;else load()}
async function extract(pid){const r=await post('/api/facts/extract',{patient_id:pid});alert('proposed '+(r.cards||[]).length+' card(s)');load()}
async function setRole(){await post('/api/settings',{operator_role:document.getElementById('rolesel').value});load()}
async function flag(){const r=await post('/api/facts/'+sel+'/flag',{should_have:document.getElementById('fb').value,category:'factual_support'});if(r.id)alert('flagged as feedback #'+r.id)}
load();
</script></body></html>"""

IMPROVE_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Chief of Health - Improve</title><style>""" + _CSS + r"""</style></head><body>""" + _NAV + r"""
<main>
<section><h2>Feedback → evaluation cases → candidate changes <small>reviewed, versioned, released by decision; nothing trains on conversations</small></h2><div id="sep" class="muted" style="font-size:12px"></div></section>
<section><h2>Feedback</h2><table id="fb"></table></section>
<section><h2>Evaluation cases</h2><table id="cases"></table></section>
<section><h2>Candidate changes</h2><div class="row" style="font-size:12px;margin-bottom:6px"><input id="c_title" placeholder="title" style="width:200px"><input id="c_desc" placeholder="what would change (prompt / rule / template / data)" style="width:340px"><input id="c_addr" placeholder="feedback ids, comma" style="width:120px"><input id="c_cases" placeholder="case ids, comma" style="width:120px"><button onclick="propose()">propose</button></div><table id="cands"></table></section>
<section><h2>Release history</h2><div id="hist" class="ev"></div></section>
</main>
<script>""" + _JS_COMMON + r"""
async function load(){const o=await (await fetch('/api/improve')).json();document.getElementById('role').textContent='planner: '+o.planner.mode;
document.getElementById('sep').textContent='Interaction logs: '+o.separation.interaction_logs+' · Evaluation cases: '+o.separation.evaluation_cases+' · Training data: '+o.separation.training_data+' · '+o.planner.note;
document.getElementById('fb').innerHTML='<tr><th>#</th><th>Target</th><th>Category</th><th>Label</th><th>Should have</th><th>Status</th><th></th></tr>'+(o.feedback.length?o.feedback.map(f=>'<tr><td>'+f.id+'</td><td>'+esc(f.target_kind||'message')+' '+esc(f.target_id||f.message_id)+' · '+esc(f.display_name)+'</td><td>'+esc(f.category||'')+'</td><td>'+esc(f.label)+'</td><td>'+esc(f.should_have)+'</td><td>'+esc(f.status)+'</td><td><button onclick="mkcase('+f.id+')">make eval case</button></td></tr>').join(''):'<tr><td colspan=7><i>none</i></td></tr>');
document.getElementById('cases').innerHTML='<tr><th>#</th><th>v</th><th>Category · target</th><th>Title</th><th>Status</th><th>Exported</th><th></th></tr>'+(o.eval_cases.length?o.eval_cases.map(c=>'<tr><td>'+c.id+'</td><td>'+c.version+(c.supersedes_id?' (was #'+c.supersedes_id+')':'')+'</td><td>'+esc(c.category)+' · '+esc(c.target_kind)+'</td><td>'+esc(c.title)+'</td><td>'+esc(c.status)+(c.reviewed_by?' by '+esc(c.reviewed_by):'')+'</td><td>'+esc(c.exported_path||'')+'</td><td><button onclick="review('+c.id+')">mark reviewed</button> <button onclick="run('+c.id+')">run</button> <button onclick="exp('+c.id+')">export</button></td></tr>').join(''):'<tr><td colspan=7><i>none</i></td></tr>');
document.getElementById('cands').innerHTML='<tr><th>#</th><th>Title</th><th>Addresses</th><th>Cases</th><th>Last evaluation</th><th>Status</th><th></th></tr>'+(o.candidate_changes.length?o.candidate_changes.map(c=>'<tr><td>'+c.id+'</td><td>'+esc(c.title)+'<br><span class="muted">'+esc(c.description)+'</span></td><td>feedback '+esc(c.addresses.join(', '))+'</td><td>'+esc(c.eval_case_ids.join(', '))+'</td><td>'+(c.eval_results?'pass '+c.eval_results.passed+' · fail '+c.eval_results.failed+' · needs review '+c.eval_results.needs_review+' @ '+esc(c.eval_results.code):'—')+'</td><td>'+esc(c.status)+(c.release_tag?' '+esc(c.release_tag):'')+'</td><td><button onclick="ev('+c.id+')">evaluate</button> <button onclick="dec('+c.id+',&quot;approve&quot;)">approve</button> <button onclick="dec('+c.id+',&quot;release&quot;)">release</button> <button onclick="dec('+c.id+',&quot;rollback&quot;)">roll back</button></td></tr>').join(''):'<tr><td colspan=7><i>none</i></td></tr>');
document.getElementById('hist').innerHTML=o.release_history.map(h=>ts(h.at)+' · '+esc(h.actor)+' · '+esc(h.action)+' candidate #'+h.candidate_id+(h.tag?' '+esc(h.tag):'')+(h.note?' · '+esc(h.note):'')).join('<br>')||'<i>none</i>'}
async function mkcase(fid){await post('/api/eval_cases/from_feedback',{feedback_id:fid});load()}
async function review(id){await post('/api/eval_cases/'+id+'/review',{status:'reviewed'});load()}
async function run(id){const r=await post('/api/eval_cases/'+id+'/run',{});alert(r.status+' — '+JSON.stringify(r.checks))}
async function exp(id){const r=await post('/api/eval_cases/'+id+'/export',{});alert('exported '+r.path);load()}
async function propose(){const ids=s=>s.split(',').map(x=>parseInt(x)).filter(x=>x);await post('/api/candidates',{title:document.getElementById('c_title').value,description:document.getElementById('c_desc').value,addresses:ids(document.getElementById('c_addr').value),case_ids:ids(document.getElementById('c_cases').value)});load()}
async function ev(id){await post('/api/candidates/'+id+'/evaluate',{});load()}
async function dec(id,a){const tag=a==='release'?prompt('release tag (commit / version)'):'';const note=a==='rollback'?prompt('rollback reason'):'';const r=await post('/api/candidates/'+id+'/decide',{action:a,tag:tag||'',note:note||''});if(!r.ok)alert(r.reason);load()}
load();
</script></body></html>"""

LOCATION_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Share your location once</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:24px;max-width:420px;margin:auto;color:#222}button{font-size:17px;padding:12px 18px;border-radius:8px;border:0;background:#1f6f43;color:#fff;width:100%}p{line-height:1.45}small{color:#666}</style></head><body>
<h2>Find the closest lab</h2>
<p>Tap the button and your phone will ask whether to share your location <b>once</b>. We use it only to pick the closest place for your lab work, then we text you the options. It is not stored with your name, and the link stops working after you use it.</p>
<button onclick="go()">Share my location once</button>
<p id="out"><small>Or reply to the text with the town or zip code where you'll be.</small></p>
<script>
async function go(){const o=document.getElementById('out');if(!navigator.geolocation){o.textContent='This phone cannot share location. Reply to the text with a town or zip code instead.';return}
navigator.geolocation.getCurrentPosition(async p=>{const r=await fetch('/api/location',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:'__TOKEN__',lat:p.coords.latitude,lon:p.coords.longitude})});const j=await r.json();o.textContent=j.ok?'Thanks. Check your texts for the closest options.':'This link is no longer valid ('+(j.reason||'')+'). Reply to the text with a town or zip code instead.'},
e=>{o.textContent='No location was shared. Reply to the text with a town or zip code instead.'},{enableHighAccuracy:false,timeout:15000,maximumAge:60000})}
</script></body></html>"""

LOCATION_EXPIRED_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Link expired</title>
<style>body{font-family:-apple-system,system-ui,sans-serif;padding:24px;max-width:420px;margin:auto;color:#222}</style></head><body>
<h2>This link has expired or was already used</h2><p>Reply to the text with the town or zip code where you'll be and we'll find the closest place.</p></body></html>"""

CONVERSATION_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Chief of Health - Conversation</title><style>""" + _CSS + r"""</style></head><body>
<header><b>Chief of Health</b><a href="/">Operations</a><a class="on" href="#">Conversation</a><span class="tag" id="who"></span><span class="tag" id="state"></span><span style="margin-left:auto" class="row"><a href="#" id="prev">&larr; previous</a><a href="#" id="next">next &rarr;</a></span></header>
<main>
<div class="phone"><div class="screen" id="screen"></div>
<div class="compose"><input id="reply" placeholder="Type what the patient would text back, then Enter" onkeydown="if(event.key==='Enter')send()"><button class="p" onclick="send()">Send as patient</button></div></div>
<div id="fbform" class="fbform" style="display:none;max-width:520px;margin:10px auto"><b>Flag message #<span id="fbmid"></span></b> - what should have happened?<br>
<select id="fblabel"><option value="defect">defect</option><option value="wording">wording</option><option value="good_example">good example</option><option value="policy_question">policy question</option></select>
<textarea id="fbshould" rows="3" style="width:100%;margin-top:6px" placeholder="Describe the reply you wanted, in the voice you want"></textarea>
<textarea id="fbnotes" rows="2" style="width:100%;margin-top:6px" placeholder="Notes (optional)"></textarea>
<div class="row" style="margin-top:6px"><button class="p" onclick="saveFb()">Save flag</button><button onclick="document.getElementById('fbform').style.display='none'">Cancel</button></div></div>
<div class="muted" style="max-width:520px;margin:8px auto;font-size:11.5px" id="foot"></div>
</main>
<script>""" + _JS_COMMON + r"""
const cid=parseInt(location.pathname.split('/').pop());let phone=null;let fbMsg=null;
function whyText(d){if(!d)return '';const keep=['rule','rule_note','reason','intent','confidence','constraints_from_message','effective_constraints','constraint_changed','sites_offered','service_requirements','filter','in_network','due_at','attempt','opener_variant','composer','composer_model','composer_refused','plan_when','rejected_site','rejected_site_reason'];const o={};for(const k of keep)if(d[k]!==undefined)o[k]=d[k];return JSON.stringify(o,null,1)}
async function load(){const t=await (await fetch('/api/conversation/'+cid)).json();if(!t.conversation){document.getElementById('screen').innerHTML='<i>no such conversation</i>';return}
const c=t.conversation;phone=c.phone;document.getElementById('who').textContent=c.display_name+(c.synthetic?'':' - NOT SYNTHETIC')+' · '+(c.phone||'no phone');document.getElementById('state').textContent=c.state;
document.getElementById('prev').href='/conversation/'+Math.max(1,cid-1);document.getElementById('next').href='/conversation/'+(cid+1);
document.getElementById('screen').innerHTML=t.messages.filter(m=>m.status!=='cancelled').map(m=>{const out=m.direction==='outbound';const held=['queued','suppressed','failed','ambiguous'].includes(m.status);return '<div class="m '+(out?'out':'in')+(held?' held':'')+'">'+esc(m.body)+'<small>'+(out?(m.composer==='anthropic'?'written by Claude':(m.composer==='fact'?'written by fact writer':'approved template'))+' · '+esc(m.template_id)+' · '+esc(m.status):'patient · '+esc(m.status))+' · '+ts(m.created_at)+(out?' <button class="flagbtn" onclick="openFb('+m.id+')">Flag</button> <button class="whybtn" onclick="tw('+m.id+')">why</button>':'')+'</small></div>'+(out&&m.decision?'<div class="why" id="why'+m.id+'" style="display:none">'+esc(whyText(m.decision))+'</div>':'')}).join('');
const sc=document.getElementById('screen');sc.scrollTop=sc.scrollHeight;
const ords=t.orders.map(o=>o.source_order_id+' ['+o.state+'] '+o.lines).join(' · ');document.getElementById('foot').textContent='Orders: '+ords+(t.escalations.length?' · Open items: '+t.escalations.filter(e=>e.status==='open').map(e=>e.reason+' → '+e.queue+(e.priority==='emergency'?' (EMERGENCY)':'')).join(', '):'')+((t.portal_messages||[]).length?' · Portal messages (patient\'s words, '+t.portal_messages[0].adapter+'): '+t.portal_messages.map(p=>'#'+p.id+' '+p.status).join(', '):'')+((t.locations||[]).length?' · Location: '+t.locations.map(l=>l.source+(l.label?' ('+l.label+')':'')).join(', '):'');}
function tw(id){const e=document.getElementById('why'+id);e.style.display=e.style.display==='none'?'block':'none'}
async function send(){const b=document.getElementById('reply').value.trim();if(!b||!phone)return;document.getElementById('reply').value='';await post('/api/sim/inbound',{phone:phone,body:b});load()}
function openFb(mid){fbMsg=mid;document.getElementById('fbmid').textContent=mid;document.getElementById('fbform').style.display='block';document.getElementById('fbshould').focus()}
async function saveFb(){const should=document.getElementById('fbshould').value.trim();if(!should)return alert('Say what should have happened.');const r=await post('/api/feedback',{message_id:fbMsg,label:document.getElementById('fblabel').value,should_have:should,notes:document.getElementById('fbnotes').value});if(r.id){document.getElementById('fbform').style.display='none';document.getElementById('fbshould').value='';document.getElementById('fbnotes').value='';await post('/api/feedback/'+r.id+'/export');alert('Saved and exported feedback #'+r.id+' with its evidence.')}}
load();setInterval(load,4000);
</script></body></html>"""
