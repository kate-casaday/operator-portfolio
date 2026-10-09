"""Deterministic keyword classifier.  SIMULATED — this is not a language model.

It exists so the workflow can be exercised end-to-end with no credentials and so
tests are reproducible.  Its token counts are *estimates* (chars/4) so the cost
instrumentation has something to add up; they are labeled simulated everywhere.
"""
from __future__ import annotations

import re
import time
from typing import Dict

from .base import ModelAdapter, ModelResult, SYSTEM_PROMPT, ProviderError

_CONFIRM_DAY_RE = re.compile(r"^\W*(mon|tue|wed|thu|fri|sat|sun)[a-z]*( morning| afternoon| evening)?"
                             r"( works| is (good|fine|best|ok)| then| it is| please)?\W*$", re.IGNORECASE)

_CONFIRM_CHOICE_RE = re.compile(r"\b(the (first|second) one|option [12]|the (bath|brunswick|topsham) one)\b", re.IGNORECASE)
_WILLING_VISIT_RE = re.compile(r"\b(stop by|drop by|come by|swing by|come in|head over)\b", re.IGNORECASE)

_RULES = [
    # (intent, barrier, regex, confidence)
    ("emergency", "clinical", r"chest pain|can'?t breathe|trouble breathing|bleeding (heavily|a lot|won'?t stop)|overdos|suicid|"
                              r"unconscious|passed out|heart attack|stroke|\b911\b", 0.95),
    ("wrong_number", "identity", r"wrong (number|person)|not (him|her|them)|who is this|don'?t know (any|a) ", 0.95),
    ("plan_changed_report", "clinical", r"(doctor|dr\.?|office|nurse|provider) (said|told me|says|called)[^.?!]{0,40}(don'?t|do not|no longer|not) need|"
                                         r"(changed|switched|swapped|replaced) (the|my|this) (test|order|labs?)|different test now|"
                                         r"(cancel+ed|dropped) (the|my|this) (test|order)|not needed anymore|no longer need(ed)?", 0.85),
    ("opt_out", "none", r"^\W*(please )?(stop|unsubscribe|quit|cancel)\b(?!\s+(by|in|at|over|off))|"
                        r"stop (texting|messaging|contacting)|leave me alone|remove me|don'?t (text|message|contact) me", 0.95),
    ("already_completed", "none", r"already (did|done|had|went|got)|did (it|them|that) (last|already|at)|"
                                   r"had (it|them|my blood) (done|drawn)|got (it|them) done|went (last|yesterday)|"
                                   r"(blood|labs?|it) (drawn|done) at|did it at|already got it done", 0.9),
    ("request_clinical_staff", "clinical", r"(talk|speak|chat) (to|with) (a |the |my )?(nurse|doctor|clinician|provider|care team)|"
                                            r"nurse (first|about|call)|call me .*(nurse|doctor)|(nurse|doctor) (call|text) me", 0.85),
    ("request_human", "other", r"real person|a human|actual person|talk to (a person|someone|somebody)|is (this|there) a (person|human)|"
                               r"someone i can (talk|speak)", 0.85),
    ("fewer_reminders", "none", r"(too many|so many) (texts|messages|reminders)|stop reminding|keep(s)? reminding|don'?t need to remind|"
                                r"once a week|fewer (texts|reminders|messages)|less (texts|often)|ease off|not so many|"
                                r"you don'?t need to keep", 0.85),
    ("clinical_question", "clinical", r"do i (still|really|even) need|why (do i|was this)|what (is|are) (this|these|it) for|"
                                       r"fasting|fast before|need to fast|medication|symptom|results?\b|diagnos|is it (safe|dangerous)|"
                                       r"my doctor said|should i (take|stop|skip|keep)|what does .* test|blood thinner|my (meds|pills|insulin|warfarin|statin)|"
                                       r"before the (draw|test)|eat or drink|coffee before|keep taking|why (did|does|do|was|is) (the |my )?(doctor|dr|provider|office|they)|"
                                       r"what('s| is| are) (this|these|it|that)( test| lab| order| blood work)? for|reason (for|why)", 0.85),
    ("cost_question", "cost", r"how much|cost|price|copay|co-pay|insurance|afford|bill\b|\$", 0.85),
    ("transport_barrier", "transport", r"no (car|ride|way to get)|can'?t (get|drive|make it) there|no transportation|"
                                        r"bus\b|too far|wheelchair|homebound|can'?t drive", 0.85),
    ("scheduling_barrier", "schedule", r"work (until|till|late)|after (\d{1,2})|evening|weekend|saturday|sunday|"
                                        r"only (free|available|have)|not (during|before)|night shift|busy (until|till)|"
                                        r"(morning|afternoon)s? (only|work)|can'?t do (morning|afternoon)s?|"
                                        r"i (only )?have (mon|tue|wed|thu|fri|sat|sun)|during the day|after work|late afternoon", 0.8),
    ("reschedule", "schedule", r"can'?t make|reschedule|something came up|move (it|my)|different (day|time)|later (this|next)", 0.8),
    ("correction", "none", r"^\W*(no|nope|wait|wait no|actually)\b[,!.]?\s*(i said|i meant|not|it'?s|that'?s|the)|i (said|meant) \w+|"
                           r"\bnot \w+day,? \w+day\b|you (said|told me|have the wrong)|wrong (day|site|place|lab)", 0.8),
    ("needs_hours", "none", r"what time|hours|when (are|is) (it|they|you) open|open (on|until|till)", 0.8),
    ("needs_location", "none", r"where|location|address|which lab|nearest|closest", 0.8),
    ("confirm_plan", "none", r"^(yes|yep|yeah|ok|okay|sure|sounds good|that works|will do|i'?ll (go|be there)|see you)\b|"
                             r"\b(i'?ll go|i will go|i can go|i could do|could do|works for me|works,|the (first|second) one|option [12]|"
                             r"the (bath|brunswick|topsham) one)\b|"
                             r"^\W*(mon|tue|wed|thu|fri|sat|sun)[a-z]*( morning| afternoon| evening)?\W*$", 0.8),
    ("willing", "none", r"\b(how do i|what do i (do|need)|ready|let'?s do|i want to|happy to|can do|i'?d like to)\b", 0.75),
    ("abusive_or_off_topic", "other", r"\b(f+u+c+k|scam|spam|sue you)\b", 0.7),
]

_TIME_RE = re.compile(r"(?:after|until|till|past|from)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_BEFORE_RE = re.compile(r"(?:before|by)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_DAY_RE = re.compile(r"\b(mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)s?\b", re.IGNORECASE)
_NEG_IN_NET = re.compile(r"\bnot (at|in) (your|the|a) (lab|riverbend|brunswick|bath)|\bnot (riverbend|brunswick|bath)\b|wasn'?t (at )?(your|riverbend)", re.IGNORECASE)
_SCHED_INTENTS = {"scheduling_barrier", "confirm_plan", "correction", "willing", "needs_location", "needs_hours", "reschedule", "transport_barrier"}
_CAREGIVER_RE = re.compile(r"\b(my (mom|mother|dad|father|husband|wife|son|daughter|parent)|i'?m (his|her|their) (daughter|son|wife|husband|"
                           r"caregiver|aide)|caregiver|i handle (his|her|their)|(he|she) is the patient|(mom|dad) is the patient)\b", re.IGNORECASE)
_WHERE_RE = re.compile(r"\bat (?:the |a )?([A-Za-z][A-Za-z .'-]{2,30}?)(?:\s+(?:in|on|last|yesterday|already|this)\b|[.,!]|$)", re.IGNORECASE)
_OUT_NET = re.compile(r"\b(quest|labcorp|hospital|urgent care|somewhere else|elsewhere|another|other lab|my (other )?doctor'?s office|"
                      r"walgreens|cvs)\b", re.IGNORECASE)


class MockAdapter(ModelAdapter):
    name = "mock"

    def __init__(self, fail_times: int = 0, latency_ms: float = 12.0):
        self.fail_times = fail_times          # for provider-failure tests
        self.latency_ms = latency_ms
        self.calls = 0

    def classify(self, context: Dict, patient_text: str, budget: int = 1) -> ModelResult:
        self.calls += 1
        t0 = time.perf_counter()
        if self.fail_times > 0:
            self.fail_times -= 1
            self.last_attempts = [self.attempt_record(self.name, "mock-rules-v1", True, "error",
                                                      latency_ms=self.latency_ms, error="simulated provider outage")]
            raise ProviderError("simulated provider outage")
        text = patient_text.lower()
        intent, barrier, conf = "unclear", "none", 0.4
        if _CONFIRM_DAY_RE.match(text) or _CONFIRM_CHOICE_RE.search(text):
            intent, barrier, conf = "confirm_plan", "none", 0.8
        elif _WILLING_VISIT_RE.search(text) and not re.search(r"\b(can'?t|cannot|won'?t)\b", text):
            intent, barrier, conf = "willing", "none", 0.75
        for i, b, pattern, c in ([] if intent in ("confirm_plan", "willing") else _RULES):
            if re.search(pattern, text, re.IGNORECASE):
                intent, barrier, conf = i, b, c
                break
        constraints: Dict = {}
        known_towns = [t for t in (context.get("known_towns") or []) if t]
        for t in known_towns:
            if re.search(r"\b%s\b" % re.escape(t.lower()), text):
                constraints["town"] = t
                break
        # version 4: a zip, a place outside the partner's towns, or "not at home" (location needed)
        z = re.search(r"\b(\d{5})\b", text)
        if z and intent in ("needs_location", "scheduling_barrier", "willing", "unclear", "confirm_plan"):
            constraints["zip"] = z.group(1)
            if intent == "unclear":
                intent, barrier, conf = "needs_location", "none", 0.8
        # "not at home / staying with family" is a location need only when the text also asks about a place;
        # "out of town until the 30th" on its own is timing, not location
        if re.search(r"\b(not (at )?home|closest to me|near me|where i am|staying (with|at|in)|visiting)\b", text) \
                and re.search(r"\b(closest|nearest|near|where|around here|anything (near|close))\b", text) \
                and intent in ("needs_location", "scheduling_barrier", "willing", "unclear"):
            constraints["where"] = "here"
            intent, barrier, conf = "needs_location", "none", 0.8
        if intent == "clinical_question":
            # medication questions are "medication" even when they mention timing before the draw
            constraints["topic"] = ("medication" if re.search(r"medication|meds|pill|insulin|warfarin|blood thinner|statin|keep taking|should i (stop|skip|take|keep)", text)
                                    else "prep" if re.search(r"fast|eat|drink|prep|before the (test|draw)|coffee|water", text)
                                    else "rationale" if re.search(r"why (did|was|is|do|does)|what('s| is| are) (this|it|that|these)( test| lab| order| blood work)? for|the reason|reason (for|why)", text)
                                    else "needed" if re.search(r"still need|really need|even need", text)
                                    else "results" if re.search(r"result", text)
                                    else "medication" if re.search(r"medication|meds|pill", text)
                                    else "symptom" if re.search(r"symptom|pain|sick", text) else "other")
            if constraints["topic"] == "symptom":
                constraints["topic"] = "symptoms"
        if _CAREGIVER_RE.search(text):
            constraints["caregiver"] = True
        if re.search(r"\b(only|the only|must|have to|can'?t do (any|another|other)|no other|nothing else)\b", text):
            constraints["absolute"] = True
        if intent == "already_completed":
            site_names = [n.lower() for n in (context.get("site_names") or [])]
            partner = (context.get("partner_name") or "").lower()
            negated = bool(_NEG_IN_NET.search(text))
            in_net = (any(n and n in text for n in site_names) or (partner and partner in text) or
                      bool(re.search(r"\b(brunswick|bath) lab\b", text))) and not negated
            if in_net:
                constraints["in_network"] = True
            elif _OUT_NET.search(text) or negated:
                constraints["in_network"] = False
            w = _WHERE_RE.search(patient_text)          # original casing for the place name
            if w:
                constraints["where"] = w.group(1).strip()[:40]
        if intent == "correction":
            constraints["corrects"] = True
        if intent == "fewer_reminders":
            constraints["reminder_frequency"] = "reduced"
        b = _BEFORE_RE.search(text)
        if b:
            hour = int(b.group(1)); minute = int(b.group(2) or 0); ampm = (b.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            constraints["before_time"] = "%02d:%02d" % (hour, minute)
        if re.search(r"\bmornings?\b", text) and "before_time" not in constraints and not re.search(r"can'?t do mornings", text):
            constraints["before_time"] = "12:00"
        if re.search(r"\b(evenings?|after work|late afternoon|nights?)\b", text):
            constraints["evening_ok"] = True
        m = _TIME_RE.search(text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            elif not ampm and hour <= 7:      # "after 6" → 18:00 heuristic
                hour += 12
            constraints["after_time"] = "%02d:%02d" % (hour, minute)
            if hour >= 17:
                constraints["evening_ok"] = True
        if re.search(r"\blate afternoon\b", text) and "after_time" not in constraints:
            constraints["after_time"] = "15:00"
        d = _DAY_RE.search(text) if intent in _SCHED_INTENTS else None
        if d:
            constraints["weekday"] = d.group(1).lower()[:3]
            if constraints["weekday"] in ("sat", "sun"):
                constraints["weekend_ok"] = True
        if "weekend" in text:
            constraints["weekend_ok"] = True
        if re.search(r"\b(first|1st|option 1)\b", text):
            constraints["site_choice"] = "1"
        elif re.search(r"\b(second|2nd|option 2)\b", text):
            constraints["site_choice"] = "2"
        msgs = self.build_messages(context, patient_text)
        est_in = (len(SYSTEM_PROMPT) + len(msgs[0]["content"])) // 4
        est_out = 40
        r = ModelResult(intent=intent, confidence=conf, barrier=barrier, constraints=constraints,
                        adapter=self.name, model="mock-rules-v1", simulated=True,
                        input_tokens=est_in, output_tokens=est_out, cache_read_tokens=0,
                        latency_ms=max(self.latency_ms, (time.perf_counter() - t0) * 1000),
                        raw={"note": "SIMULATED classification; token counts are chars/4 estimates"})
        self.last_attempts = [self.attempt_record(self.name, r.model, True, "ok", r)]
        return r
