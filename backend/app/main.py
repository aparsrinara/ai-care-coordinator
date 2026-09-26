"""AI Care Coordinator demo API.

Each visitor gets their own demo state, keyed by the X-Session-Id header (the
UI generates a random id and keeps it in localStorage). State lives in memory
and resets when the server restarts.
"""
import hashlib
import io
import json
import logging
import re
import threading
import time
from collections import OrderedDict

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import demo_data as D  # noqa: E402
from . import costs, fhir_data, insurance, llm  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("care")

app = FastAPI(title="AI Care Coordinator API", version="0.1.0",
              description="Demo backend. All people, providers, prices and plan details are sample data.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ---------- per-visitor state ----------

MAX_SESSIONS = 1000
SESSION_TTL_SECONDS = 12 * 3600
MAX_DOC_CHARS = 30000
_sessions: OrderedDict[str, dict] = OrderedDict()
_lock = threading.Lock()


def _fresh() -> dict:
    return {
        "account": {"name": "Maya Chen", "dob": "1991-03-04", "email": "maya.chen@example.com", "phone": "(555) 014-2291"},
        "insurance": insurance.sample(),
        "links": {"plan": False, "portal": False, "email": False},
        "emailProvider": None,
        "disputeAt": None,
        "authorizedRep": False,
        "ptApprovedAt": None,
        "labsResulted": False,
        "autoApprove": False,
        "docs": [dict(d) for d in D.DEFAULT_DOCS],
        "docText": "",
        "chat": [],
        "_seen": time.time(),
    }


def session(sid: str | None) -> dict:
    sid = (sid or "default")[:64]
    now = time.time()
    with _lock:
        # Oldest-used first, so expired sessions are at the front.
        while _sessions:
            oldest = next(iter(_sessions.values()))
            if now - oldest["_seen"] < SESSION_TTL_SECONDS and len(_sessions) <= MAX_SESSIONS:
                break
            _sessions.popitem(last=False)
        if sid not in _sessions:
            _sessions[sid] = _fresh()
        _sessions.move_to_end(sid)
        _sessions[sid]["_seen"] = now
        return _sessions[sid]


def pt_stage(s: dict) -> int:
    """0 = needs the member's OK, 1-4 = in progress, 5 = approved and booked."""
    if s["ptApprovedAt"] is None:
        return 0
    stage = 1 + int((time.time() - s["ptApprovedAt"]) / D.PT_STEP_SECONDS)
    return 5 if stage >= 4 else stage  # the 4th step (booking) finishing means booked


def dispute_stage(s: dict) -> int:
    """0 = issue found, 1 = disputing, 2 = fixed (corrected bill received)."""
    if s["disputeAt"] is None:
        return 0
    return 2 if time.time() - s["disputeAt"] >= D.DISPUTE_SECONDS else 1


def _bill_view(s: dict, bill: dict) -> dict:
    b = _t(bill, s)
    if bill["id"] == "lab":
        stage = dispute_stage(s)
        b["amount"] = 185 if stage == 2 else 370
        b["status"] = [["Issue found", "warn"], ["Disputing", "plum"], ["Fixed · OK to pay", "ok"]][stage]
        b["stage"] = stage
        b["tappable"] = True
        if stage == 2:
            b["lines"] = b["lines"][:1]
    else:
        b["amount"] = bill["billed"]
    return b


def _money(n) -> str:
    return f"${n:,.0f}" if n is not None else "deductible + coinsurance"


def _tokens(s: dict) -> dict:
    acct, ins = s["account"], s["insurance"]
    b = ins["benefits"]
    try:
        y, m, d = map(int, acct["dob"].split("-"))
        dob_spoken = f"{['January','February','March','April','May','June','July','August','September','October','November','December'][m - 1]} {d}, {y}"
    except (ValueError, IndexError):
        dob_spoken = acct["dob"]
    pref, non = b.get("genericRx90Copay"), b.get("genericRx90NonPreferred")
    return {
        "name": acct["name"], "first": acct["name"].split()[0] if acct["name"].strip() else "there",
        "dob_spoken": dob_spoken, "payer": ins["payer"], "plan_type": ins["planType"],
        "payer_initials": "".join(w[0] for w in ins["payer"].split()[:2]).upper(),
        "rx_pref": _money(pref), "rx_non": _money(non),
        "rx_saved": _money(non - pref) if pref is not None and non is not None else "money",
    }


def _t(obj, s: dict):
    """Fill {tokens} in demo text (recursively) for this member."""
    if isinstance(obj, str):
        for k, v in _tokens(s).items():
            obj = obj.replace("{" + k + "}", str(v))
        return obj
    if isinstance(obj, list):
        return [_t(x, s) for x in obj]
    if isinstance(obj, dict):
        return {k: _t(v, s) for k, v in obj.items()}
    return obj


def _estimate(s: dict, service_id: str) -> dict:
    b = s["insurance"]["benefits"]
    return costs.estimate(service_id, insurance.engine(s["insurance"]), b["deductibleMet"], b["oopMet"])


# ---------- visit summary (generated once from the FHIR visit note) ----------

FALLBACK_SUMMARY = ("Your checkup looked good overall. Dr. Nair wants fasting labs to recheck cholesterol, "
                    "a 90-day refill of your current medication, a skin check for one mole, and physical therapy for your knee.")
_visit_summary = {"text": FALLBACK_SUMMARY, "source": "fallback"}


def _generate_summary():
    result = llm.summarize_visit(fhir_data.VISIT_NOTE)
    if result:
        _visit_summary.update(text=result["summary"], source="bedrock")
        log.info("Visit summary generated by Bedrock")


# ---------- lab results (explained once, shown after the demo fast-forwards) ----------

def _range_text(o: dict) -> str:
    r = o["referenceRange"][0]
    unit = o["valueQuantity"]["unit"]
    if "low" in r and "high" in r:
        return f"{r['low']['value']}-{r['high']['value']} {unit}"
    return f"under {r['high']['value']} {unit}" if "high" in r else f"over {r['low']['value']} {unit}"


def _lab_rows() -> list[dict]:
    rows = []
    for o in fhir_data.LAB_OBSERVATIONS:
        row = {"id": o["id"], "name": o["code"]["text"], "loinc": o["code"]["coding"][0]["code"],
               "value": o["valueQuantity"]["value"], "unit": o["valueQuantity"]["unit"],
               "range": _range_text(o), "flag": o["interpretation"][0]["text"]}
        if o["code"]["coding"][0]["code"] == fhir_data.PRIOR_LDL["code"]["coding"][0]["code"]:
            row["previous"] = {"value": fhir_data.PRIOR_LDL["valueQuantity"]["value"], "date": "2025-09-18"}
        rows.append(row)
    return rows


FALLBACK_RESULTS = ("Your LDL (\"bad\") cholesterol came down from 142 to 118. It's still above the target of 100, "
                    "and your other results are in the normal range. Dr. Nair says to keep taking atorvastatin 10 mg "
                    "and recheck in 12 months. Questions go to Dr. Nair.")
_results_explained = {"text": FALLBACK_RESULTS, "source": "fallback"}


def _generate_results():
    lines = [f"{r['name']}: {r['value']} {r['unit']} (normal {r['range']}, {r['flag']})"
             + (f", previous {r['previous']['value']} on {r['previous']['date']}" if "previous" in r else "")
             for r in _lab_rows()]
    text = llm.explain_results("\n".join(lines) + "\n\nDoctor's comment: " + fhir_data.DIAGNOSTIC_REPORT["conclusion"])
    if text:
        _results_explained.update(text=text, source="bedrock")
        log.info("Lab results explained by Bedrock")


@app.on_event("startup")
def _startup():
    threading.Thread(target=_generate_summary, daemon=True).start()
    threading.Thread(target=_generate_results, daemon=True).start()


# ---------- general ----------

@app.get("/api/health")
def health():
    return {"ok": True, "model": llm.MODEL_ID, "visitSummary": _visit_summary["source"]}


@app.get("/api/metrics")
def metrics():
    """LLM usage since the server started."""
    st = llm.STATS
    return {**st, "avgSeconds": round(st["seconds"] / st["calls"], 2) if st["calls"] else None,
            "chatCache": {**CACHE_STATS, "entries": len(_reply_cache)}, "sessions": len(_sessions)}


@app.post("/api/reset")
def reset(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    s.clear()
    s.update(_fresh())
    return {"ok": True}


@app.get("/api/fhir")
def fhir_bundle(x_session_id: str | None = Header(None)):
    """The raw FHIR R4 record everything else is built from."""
    if not session(x_session_id)["labsResulted"]:
        return fhir_data.BUNDLE
    extra = [fhir_data.DIAGNOSTIC_REPORT, *fhir_data.LAB_OBSERVATIONS]
    return dict(fhir_data.BUNDLE, entry=fhir_data.BUNDLE["entry"] + [{"resource": r} for r in extra])


@app.post("/api/demo/results-in")
def results_in(x_session_id: str | None = Header(None)):
    """Demo fast-forward: pretend it's Tuesday and the Monday lab results were released."""
    session(x_session_id)["labsResulted"] = True
    return {"labsResulted": True}


# ---------- 1. sign up + link accounts ----------

class Signup(BaseModel):
    name: str
    dob: str
    email: str
    phone: str | None = None


@app.post("/api/signup")
def signup(body: Signup, x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    s["account"] = body.model_dump()
    return {"account": s["account"]}


@app.get("/api/plans")
def plans(q: str = ""):
    """Suggestions for the plan search box. Any payer name is accepted by POST /api/insurance."""
    q = q.lower()
    return {"payers": [dict(p, initials="".join(w[0] for w in p["name"].split()[:2]).upper())
                       for p in insurance.PAYER_DIRECTORY if q in (p["name"] + p["subtitle"]).lower()],
            "planTypes": insurance.PLAN_TYPES, "benefitFields": insurance.BENEFIT_FIELDS}


class InsuranceIn(BaseModel):
    payer: str
    planName: str | None = None
    planType: str | None = None  # PPO, HMO, EPO, POS, HDHP, Medicare Advantage, Medicaid
    memberName: str | None = None
    memberId: str | None = None
    groupNumber: str | None = None
    rxBin: str | None = None
    coverage: str | None = None  # "Just you", "You + spouse", ...
    coverageSource: str | None = None  # employer, marketplace, medicare, medicaid
    premiumMonthly: float | None = None
    benefits: dict | None = None  # any of insurance.BENEFIT_FIELDS, e.g. {"deductible": 2000, "specialistCopay": 60}


def _insurance_view(s: dict) -> dict:
    ins = s["insurance"]
    acct_name = s["account"]["name"].strip().lower()
    card_name = (ins["memberName"] or "").strip().lower()
    return {
        "insurance": {k: v for k, v in ins.items()},
        "nameMatchesAccount": (not card_name) or card_name == acct_name,
        "estimatedFields": [k for k, v in ins["benefitSources"].items() if v in ("typical", "assumed")],
        "tip": None if ins["source"] == "sample" else
               "Numbers marked typical are averages for this plan type. Upload your Summary of Benefits and Coverage for your exact plan.",
    }


@app.post("/api/insurance")
def set_insurance(body: InsuranceIn, x_session_id: str | None = Header(None)):
    """Member types in their own insurance. Anything they skip falls back to typical values for the plan type."""
    s = session(x_session_id)
    s["insurance"] = insurance.from_entry(body.model_dump())
    return _insurance_view(s)


@app.get("/api/insurance")
def get_insurance(x_session_id: str | None = Header(None)):
    return _insurance_view(session(x_session_id))


@app.get("/api/me")
def me(x_session_id: str | None = Header(None)):
    """Profile screen: the account, the plan and what's connected."""
    s = session(x_session_id)
    ins = s["insurance"]
    return {
        "account": s["account"],
        "insurance": {"payer": ins["payer"], "planName": ins["planName"], "planType": ins["planType"],
                      "memberId": ins["memberId"], "source": ins["source"],
                      "estimatedFields": [k for k, v in ins["benefitSources"].items() if v in ("typical", "assumed")]},
        "connections": [{"name": _t(D.CONNECTIONS[k]["org"], s) if k in D.CONNECTIONS else f"Email ({s['emailProvider'] or 'not linked'})",
                         "linked": v} for k, v in s["links"].items()],
        "authorizedRep": s["authorizedRep"],
    }


@app.post("/api/card/scan")
async def scan_card(file: UploadFile | None = File(None), x_session_id: str | None = Header(None)):
    """'Use the sample card' path: loads Maya's sample Blue Ridge plan. (Real card OCR is not built yet.)"""
    s = session(x_session_id)
    s["insurance"] = insurance.sample()
    ins = s["insurance"]
    return {"card": {"payer": ins["payer"], "plan": ins["planName"], "planType": ins["planType"],
                     "memberName": ins["memberName"], "memberId": ins["memberId"], "group": ins["groupNumber"],
                     "rxBin": ins["rxBin"]}, **_insurance_view(s)}


@app.get("/api/connections")
def connections(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    found = [dict(f, linked=s["links"].get(f["id"], False)) for f in D.FOUND_FROM_CLAIMS]
    return {
        "links": s["links"],
        "authorizedRep": s["authorizedRep"],
        "sources": {k: dict(_t(v, s), linked=s["links"][k]) for k, v in D.CONNECTIONS.items()},
        "foundFromClaims": found if s["links"]["plan"] else [],
        "readyToFinish": s["links"]["plan"] and s["links"]["portal"] and s["authorizedRep"],
    }


@app.post("/api/connections/{source}")
def link(source: str, body: dict | None = None, x_session_id: str | None = Header(None)):
    """Simulated OAuth sign-in. In production this is the SMART on FHIR authorization-code flow.
    `email` takes {"provider": "Gmail" | "Outlook" | "Other"}."""
    if source == "email":
        return link_email(EmailLink(**(body or {})), x_session_id)
    if source not in D.CONNECTIONS:
        raise HTTPException(404, f"Unknown source '{source}'. Use: {', '.join(D.CONNECTIONS)}, email")
    s = session(x_session_id)
    s["links"][source] = True
    return {"linked": source, "org": _t(D.CONNECTIONS[source]["org"], s), "links": s["links"]}


class EmailLink(BaseModel):
    provider: str = "Gmail"  # Gmail, Outlook, Other


@app.get("/api/connections/email")
def email_info(x_session_id: str | None = Header(None)):
    """What the email connection will and won't do, for the consent screen."""
    s = session(x_session_id)
    return {
        "providers": ["Gmail", "Outlook", "Other"],
        "senders": _t(D.BILLING_SENDERS, s),
        "will": [f"Search only for emails from {', '.join(_t(D.BILLING_SENDERS, s)[:-1])} and {_t(D.BILLING_SENDERS, s)[-1]}",
                 "Check each bill against what your insurance says you owe"],
        "wont": ["Open, store or summarize any other email", "Send, delete or change anything in your inbox"],
        "note": "Email providers give apps read access to your whole inbox, not just certain senders. "
                "In production this is Gmail or Microsoft Graph read-only access, which requires the provider's security review.",
        "linked": s["links"]["email"],
    }


def link_email(body: EmailLink | None = None, x_session_id: str | None = Header(None)):
    """Simulated read-only email connection. Finds provider bills and checks them against EOBs."""
    s = session(x_session_id)
    s["links"]["email"] = True
    s["emailProvider"] = (body.provider if body else "Gmail")
    return {"linked": "email", "billsFound": len(D.BILLS), "message": f"Email linked. Found {len(D.BILLS)} bills, checking them now."}


@app.post("/api/consent/authorized-rep")
def authorize_rep(x_session_id: str | None = Header(None)):
    """Member e-signs a HIPAA authorization + authorized-representative form so the
    coordinator can call offices, talk to the plan and dispute bills for them."""
    s = session(x_session_id)
    s["authorizedRep"] = True
    return {"authorizedRep": True}


# ---------- 2. today ----------

@app.get("/api/today")
def today(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    stage = pt_stage(s)
    done = 4 if stage >= 5 else 3
    if stage >= 5:
        msg = "All 4 next steps from Dr. Nair's plan are handled. Nothing needs you right now."
    elif stage > 0:
        msg = "3 of 4 next steps are handled. I'm working on your PT approval now."
    else:
        msg = "I handled 3 of 4 next steps from Dr. Nair's plan. One needs your OK."
    upcoming = _t(D.UPCOMING, s) + ([D.PT_UPCOMING] if stage >= 5 else [])
    upcoming.sort(key=lambda u: u["date"])
    return {
        "dateLabel": D.DEMO_TODAY_LABEL,
        "greeting": f"Good morning, {s['account']['name'].split()[0]}",
        "coordinator": {"since": "since Tuesday's physical", "message": msg, "stepsDone": done, "stepsTotal": 4,
                        "pendingApprovals": 1 if stage == 0 else 0},
        "upcoming": [u for u in upcoming if not (s["labsResulted"] and u["title"] == "Fasting blood work")],
        "newResults": {"title": "Your cholesterol results are in", "detail": "LDL down from 142 to 118. Dr. Nair: keep the current dose.",
                       "href": "/api/results"} if s["labsResulted"] else None,
        "synced": [{"name": "Northside Health", "detail": "synced 2h ago"}, {"name": s["insurance"]["payer"]}],
    }


# ---------- 3. visit summary + PT approval ----------

def _pt_step(s: dict) -> dict:
    stage = pt_stage(s)
    if stage == 0:
        status, detail = "needs_approval", "Your plan needs prior authorization. I'll ask Dr. Nair's office to submit it. Review and approve."
    elif stage < 5:
        status, detail = "in_progress", "Dr. Nair's office submitted the request. I'm tracking it with your health plan."
    else:
        status, detail = "booked", "Approved for 12 visits. First visit Tue Oct 6, 5:30 PM at Motion PT."
    return {"id": "pt", "title": "Physical therapy", "status": status, "channel": "portal_message", "detail": detail}


@app.get("/api/visits/latest")
def latest_visit(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    steps = [dict(_t(v, s), id=k) for k, v in D.NEXT_STEPS.items()] + [_pt_step(s)]
    if s["labsResulted"]:
        steps[0].update(status="resulted", detail="Results are in. Dr. Nair reviewed them: keep the current dose, recheck in 12 months.")
    return {
        "encounterId": fhir_data.ENCOUNTER["id"],
        "title": "Annual physical",
        "provider": "Dr. Priya Nair · Primary care · Sep 22",
        "summary": _visit_summary["text"],
        "summarySource": _visit_summary["source"],
        "disclaimer": "From your after-visit notes, synced from your patient portal. I don't give medical advice. Questions about results go to Dr. Nair.",
        "stepsDone": sum(1 for st in steps if st["status"] in ("booked", "ordered", "resulted")),
        "steps": steps,
    }


def _projected_total(s: dict, service_id: str, count: int) -> float:
    """Total for `count` visits, applying the deductible and OOP max as they fill up."""
    b = s["insurance"]["benefits"]
    t = costs.Tracker(insurance.engine(s["insurance"]), b["deductibleMet"], b["oopMet"])
    _, provider_id, price = costs.SERVICES[service_id]["options"][0]
    for _ in range(count):
        t.charge(price, costs.SERVICES[service_id]["category"], provider_id)
    return round(t.total_paid, 2)


def _pt_view(s: dict) -> dict:
    stage = pt_stage(s)
    pt = _estimate(s, "pt_visit")["cheapest"]
    per_visit = pt["youPay"]
    timeline = [dict(_t(step, s), state="done" if i < stage else "busy" if i == stage and stage < 5 else "waiting")
                for i, step in enumerate(D.PT_STEPS)]
    return {
        "stage": stage,
        "status": _pt_step(s)["status"],
        "plan": [
            "Ask Dr. Nair's office to submit the prior authorization. They already have your knee notes.",
            "Track it with your health plan every 2 days, and appeal if it's denied.",
            "Book your first visit at Motion PT (in-network, 1.2 mi) once it's approved.",
        ],
        "cost": {"perVisit": per_visit, "breakdown": pt["breakdown"], "visits": 12,
                 "estimatedTotal": _projected_total(s, "pt_visit", 12),
                 "oopLeft": _estimate(s, "pt_visit")["oopLeft"]},
        "privacy": _t("Your request goes to Dr. Nair's office through your patient portal. The office submits it to {payer}.", s),
        "timeline": timeline,
        "timingNote": "Demo timing is sped up. Real approvals usually take 2 to 10 business days, and I follow up every 2 days.",
    }


@app.get("/api/visits/latest/pt")
def pt_status(x_session_id: str | None = Header(None)):
    """Poll this every ~2s after approving to animate the timeline."""
    return _pt_view(session(x_session_id))


class Approve(BaseModel):
    autoApprove: bool = False


@app.post("/api/visits/latest/pt/approve")
def pt_approve(body: Approve | None = None, x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    if s["ptApprovedAt"] is None:
        s["ptApprovedAt"] = time.time()
    s["autoApprove"] = bool(body and body.autoApprove)
    return _pt_view(s)


# ---------- 4. booking call ----------

@app.get("/api/calls/{call_id}")
def call(call_id: str, x_session_id: str | None = Header(None)):
    if call_id != D.CALL["id"]:
        raise HTTPException(404, "Call not found")
    return _t(D.CALL, session(x_session_id))


# ---------- 5. coverage + documents ----------

@app.get("/api/coverage")
def coverage(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    ins = s["insurance"]
    b, src = ins["benefits"], ins["benefitSources"]
    meters = [
        {"label": "Deductible", "used": b["deductibleMet"], "total": b["deductible"],
         "text": f"${b['deductibleMet']:,.0f} of ${b['deductible']:,.0f}", "estimated": src["deductible"] in ("typical", "assumed")},
        {"label": "Out-of-pocket max", "used": b["oopMet"], "total": b["oopMax"],
         "text": f"${b['oopMet']:,.0f} of ${b['oopMax']:,.0f}", "estimated": src["oopMax"] in ("typical", "assumed")},
    ]
    if ins["source"] == "sample":
        meters.append({"label": "FSA", "used": 860, "total": 1500, "text": "$640 left · resets Dec 31", "estimated": False})
    return {
        "plan": ins["planName"], "planType": ins["planType"], "payer": ins["payer"], "coverage": ins["coverage"],
        "memberId": ins["memberId"], "source": ins["source"],
        "estimatedFields": [k for k, v in src.items() if v in ("typical", "assumed")],
        "meters": meters,
        "emailLinked": s["links"]["email"],
        "alerts": _coverage_alerts(s),
        "bills": [_bill_view(s, b) for b in D.BILLS] if s["links"]["email"] else [],
        "billingSenders": len(D.BILLING_SENDERS) + 1 if s["links"]["email"] else 0,
        "documents": [{k: v for k, v in d.items() if k != "text"} for d in s["docs"]],
    }


def _coverage_alerts(s: dict) -> list[dict]:
    if not s["links"]["email"]:
        return []  # the duplicate shows up on the provider's bill, which we only see through email
    stage = dispute_stage(s)
    if stage == 2:
        return [{"kind": "fixed", "tone": "ok", "billId": "lab", "title": "Fixed: you saved $185",
                 "detail": "Northside Lab corrected your Aug 14 bill to $185, matching your insurance."}]
    detail = ("Northside Lab's bill lists the same Aug 14 blood test twice ($370). Your insurance says you owe $185."
              + (" I've asked for a corrected bill." if stage == 1 else " Tap to review."))
    return [{"kind": "duplicate_charge", "tone": "warn", "billId": "lab", "title": "Caught a duplicate charge",
             "detail": detail, "evidence": [e["id"] for e in fhir_data.EXPLANATIONS_OF_BENEFIT]}]


@app.get("/api/bills/{bill_id}")
def bill_detail(bill_id: str, x_session_id: str | None = Header(None)):
    """A provider bill next to the plan's EOB for the same service."""
    s = session(x_session_id)
    bill = next((b for b in D.BILLS if b["id"] == bill_id), None)
    if not bill:
        raise HTTPException(404, "Bill not found")
    v = _bill_view(s, bill)
    if bill_id == "lab":
        stage = v["stage"]
        v["title"] = "Corrected bill: you owe $185" if stage == 2 else "Northside Lab billed the same test twice"
        v["explanation"] = ("The billing office removed the duplicate line. The corrected bill matches your insurance, so it is safe to pay."
                            if stage == 2 else
                            "Same test code on the same day, listed twice. Your insurance processed one test, so you should owe $185, not $370.")
        v["plan"] = "Send Northside Lab's billing office your EOB and ask for a corrected bill. I'll follow up every 5 days until it's fixed."
        v["planNote"] = "Hold off paying for now. Billing offices usually pause the bill while they review it."
        v["progress"] = [None,
                         {"title": "Request sent to Northside Lab billing", "detail": "Your EOB is attached. Waiting for their corrected bill."},
                         {"title": "Saved you $185", "detail": "Due Oct 20. I'll remind you a few days before."}][stage]
        v["timingNote"] = "Demo timing is sped up. Real corrections usually take 1 to 3 weeks." if stage == 1 else None
    return v


@app.post("/api/bills/{bill_id}/dispute")
def dispute(bill_id: str, x_session_id: str | None = Header(None)):
    """Ask the provider's billing office for a corrected bill (uses the member's HIPAA authorization)."""
    if bill_id != "lab":
        raise HTTPException(400, "Nothing to dispute on this bill")
    s = session(x_session_id)
    if s["disputeAt"] is None:
        s["disputeAt"] = time.time()
    return bill_detail(bill_id, x_session_id)


def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages[:20], 1):
        out.append(f"[page {i}] {page.extract_text() or ''}")
        if sum(map(len, out)) > MAX_DOC_CHARS:
            break
    return "\n".join(out), len(reader.pages)


@app.post("/api/documents")
async def upload(file: UploadFile = File(...), x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    data = await file.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "File is over 15 MB")
    name = file.filename or "Document"
    ctype = file.content_type or ""
    text = ""
    if ctype == "application/pdf" or name.lower().endswith(".pdf"):
        try:
            text, pages = _pdf_text(data)
            meta = f"PDF · {pages} page{'s' if pages != 1 else ''} · read"
        except Exception:
            log.exception("PDF read failed")
            meta = "Saved · could not read the text"
    elif ctype.startswith("text/") or name.lower().endswith(".txt"):
        text = data.decode("utf-8", "ignore")[:MAX_DOC_CHARS]
        meta = "Text · read"
    else:
        meta = "Photo · checking against your EOBs"
    readable = len(text.strip()) > 40
    if text and not readable:
        meta = "Saved · looks scanned, could not read the text"
    if readable:
        s["docText"] = (s["docText"] + f"\n\n=== {name} ===\n{text}")[-MAX_DOC_CHARS:]
    doc = {"name": name, "meta": meta, "icon": "card" if ctype.startswith("image/") else "doc", "new": True}
    s["docs"].insert(0, doc)
    message = ("Read it. Ask me anything about this plan." if readable else
               "Got it. I'll check it against your insurance." if ctype.startswith("image/") else "Saved to your documents")
    return {"document": doc, "readable": readable, "message": message}


@app.post("/api/insurance/document")
async def plan_document(file: UploadFile = File(...), x_session_id: str | None = Header(None)):
    """Upload a Summary of Benefits and Coverage (PDF or text). Benefits are read from it and replace
    the typical values; the text is also available to the cost chat."""
    result = await upload(file, x_session_id)
    s = session(x_session_id)
    if not result["readable"]:
        return {**result, "updatedFields": [], "message": "Saved, but I couldn't read text from it (a scanned PDF?)."}
    text = s["docText"].split(f"=== {result['document']['name']} ===", 1)[-1]
    extracted = insurance.extract_benefits(text)
    if not extracted:
        return {**result, "updatedFields": [], "message": "Saved. I couldn't find plan benefits in it, but I can still answer questions from it."}
    if s["insurance"]["source"] == "sample":
        entry = {k: extracted.get(k) for k in ("payer", "planName", "planType")}
        entry["payer"] = entry["payer"] or "Your health plan"
        s["insurance"] = insurance.from_entry(entry)
    updated = insurance.apply_document(s["insurance"], extracted, result["document"]["name"])
    result["document"]["meta"] += f" · {len(updated)} benefits found"
    return {**result, "updatedFields": updated, "extracted": extracted, **_insurance_view(s),
            "message": f"Read your plan. Updated {len(updated)} benefits, so cost answers now use your exact numbers."}


# ---------- lab results ----------

@app.get("/api/results")
def results(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    if not s["labsResulted"]:
        return {"status": "pending", "detail": "Fasting labs are Mon 9/28. Results usually post in 1-2 days.",
                "demoHint": "POST /api/demo/results-in to fast-forward"}
    report = fhir_data.DIAGNOSTIC_REPORT
    return {
        "status": "final", "reportId": report["id"], "title": "Cholesterol and metabolic panel",
        "collected": "Mon Sep 28 · Northside Lab", "released": "Tue Sep 29",
        "explanation": _results_explained["text"], "explanationSource": _results_explained["source"],
        "doctorComment": report["conclusion"],
        "results": _lab_rows(),
        "disclaimer": "I explain results, I don't interpret them. Questions go to Dr. Nair.",
        "nextActions": [{"id": "message-doctor", "label": "Message Dr. Nair"},
                        {"id": "remind-recheck", "label": "Remind me to recheck in Sep 2027"}],
    }


# ---------- cost estimates (computed, not generated) ----------

def _estimates_note(s: dict) -> str | None:
    if s["insurance"]["source"] == "sample":
        return None
    return "Prices are typical in-network rates, not your plan's negotiated rates. Benefits marked typical are estimates."


@app.get("/api/estimates")
def estimates(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    b = s["insurance"]["benefits"]
    return {"asOf": D.DEMO_TODAY, "note": _estimates_note(s),
            "estimates": costs.all_estimates(insurance.engine(s["insurance"]), b["deductibleMet"], b["oopMet"])}


@app.get("/api/estimates/{service_id}")
def estimate(service_id: str, x_session_id: str | None = Header(None)):
    if service_id not in costs.SERVICES:
        raise HTTPException(404, f"Unknown service. Use one of: {', '.join(costs.SERVICES)}")
    s = session(x_session_id)
    return dict(_estimate(s, service_id), note=_estimates_note(s))


# ---------- open enrollment ----------

@app.get("/api/enrollment/2027")
def enrollment(x_session_id: str | None = Header(None)):
    """Next year's plan options, priced against the member's care this year."""
    return _enrollment_for(session(x_session_id))


def _enrollment_for(s: dict) -> dict:
    ins = s["insurance"]
    window = insurance.ENROLLMENT_WINDOWS[ins["coverageSource"]]
    if window is None:
        return {"window": None, "headline": "Medicaid doesn't have an open enrollment period. Your coverage renews each year; "
                "I'll remind you when your renewal paperwork is due.", "options": [], "recommended": None}
    cmp = costs.compare_plans(insurance.alternatives(ins), "current")
    rec = next(o for o in cmp["options"] if o["planId"] == cmp["recommended"])
    cheapest = cmp["options"][0]
    if rec["current"]:
        headline = "Your current plan is still your best option for next year, based on this year's care."
    else:
        headline = f"Switching to {rec['name']} would save you about ${-rec['vsCurrent']:,.0f} next year and keep all your doctors."
    if cheapest["planId"] != rec["planId"]:
        headline += (f" {cheapest['name']} costs less, but {', '.join(cheapest['outOfNetworkForYou'])} "
                     f"would be out of network.")
    return {"window": window, "headline": headline, **cmp,
            "caveat": "Based on this year's care. A bigger year (surgery, a new diagnosis) changes the math, so check each plan's worst case. "
                      "Other plans use typical premiums and benefits until next year's plan documents are out."}


# ---------- 6. ask about costs ----------

CHAT_RULES = """You are the AI care coordinator in a health app. Answer the member's questions about what care will cost, what their plan covers and how to use their benefits, using only the plan data, record and documents below. Today is Sep 25, 2026.
Rules:
- First line: the direct answer, with the dollar amount in **bold**. When there are several places to get care, lead with the cheapest in-network option.
- Money: copy the amounts and breakdown lines from COST ESTIMATES word for word. Never add, change or invent a charge.
- Then 2 to 4 short "- " bullets showing the math (price, deductible left, copay or coinsurance) or the key facts.
- Add a cheaper in-network option or a benefit tip when there is one. You may offer to book or remind.
- Never give medical advice or say whether they need care. For clinical questions, say to ask Dr. Nair. For emergencies, say to call 911.
- If the data doesn't answer it, say what you'd check (for example, calling the plan) instead of guessing.
- If PLAN DATA lists estimatedFields, those numbers are typical for the plan type, not confirmed. When an answer depends on one, say it's an estimate and suggest uploading their Summary of Benefits.
- Under 90 words. Plain text only: "- " bullets and **bold**, no headings, no tables.

PLAN DATA (JSON):
{plan}"""


FALLBACK_SERVICES = [
    (r"mri|imaging", "knee_mri"), (r"physical|\bpt\b|therap", "pt_visit"), (r"urgent|walk.?in", "urgent_care"),
    (r"\ber\b|emergency", "er_visit"), (r"refill|pharm|prescri|\bmed|drug", "generic_90day"),
    (r"derm|skin|mole|specialist", "derm_new_visit"), (r"primary|checkup|doctor visit", "primary_care"),
]


# ---------- token budget: only send the model what this question needs ----------

ENROLL_RE = r"switch|next year|2027|enroll|change (my )?plan|better plan|which plan|hsa|hmo|ppo"
CARE_RE = r"\bpt\b|therap|refill|pharm|derm|skin|lab|book|appoint|approv|result"
STOPWORDS = set("what will much does cost costs have with that this from your about there their would could should "
                "when where which plan my the and for are how can get".split())
DOC_CHUNK = 1200
DOC_TOP_K = 2


def _matched_services(q: str) -> list[str]:
    return [sid for pattern, sid in FALLBACK_SERVICES if re.search(pattern, q, re.I)]


def _doc_excerpts(doc_text: str, q: str) -> str:
    """Keyword retrieval: the few document chunks that mention the question's words."""
    words = {w for w in re.findall(r"[a-z]{4,}", q.lower()) if w not in STOPWORDS}
    if not doc_text or not words:
        return ""
    chunks = [doc_text[i:i + DOC_CHUNK] for i in range(0, len(doc_text), DOC_CHUNK)]
    scored = sorted(((sum(c.lower().count(w) for w in words), i) for i, c in enumerate(chunks)), reverse=True)
    picked = sorted(i for score, i in scored[:DOC_TOP_K] if score > 0)
    return "\n...\n".join(chunks[i] for i in picked)


def _chat_system(s: dict, q: str) -> str:
    ins = s["insurance"]
    plan = {k: v for k, v in insurance.display(ins).items() if v not in (None, [], {})}
    system = CHAT_RULES.format(plan=json.dumps(plan, separators=(",", ":")))

    if re.search(CARE_RE, q, re.I):
        care = [f"- {v['title']}: {v['status']}. {v['detail']}" for v in _t(list(D.NEXT_STEPS.values()), s)]
        care.append(f"- Physical therapy: {_pt_step(s)['detail']}")
        if pt_stage(s) < 5:
            care.append("- PT is not approved yet, so don't say it's booked.")
        system += "\n\nCURRENT CARE:\n" + "\n".join(care)

    b = ins["benefits"]
    eng = insurance.engine(ins)
    matched = _matched_services(q)
    if matched:  # full breakdown, only for the services asked about
        lines = []
        for sid in matched:
            e = costs.estimate(sid, eng, b["deductibleMet"], b["oopMet"])
            opts = "; ".join(f"{o['provider']}: you pay ${o['youPay']:,.0f} ("
                             + ", ".join(f"{x['label']} ${x['amount']:,.0f}" for x in o["breakdown"]) + ")"
                             for o in e["options"])
            lines.append(f"- {e['service']}{' (needs prior authorization)' if e['requiresPriorAuth'] else ''}: {opts}")
    else:  # one line per service so general questions still have numbers
        lines = [f"- {e['service']}: ${e['cheapest']['youPay']:,.0f} at {e['cheapest']['provider']}"
                 for e in costs.all_estimates(eng, b["deductibleMet"], b["oopMet"])]
    system += ("\n\nCOST ESTIMATES from the app's cost engine (exact: quote these numbers and this math, "
               "never recompute them):\n" + "\n".join(lines))
    if _estimates_note(s):
        system += "\n(" + _estimates_note(s) + ")"

    if re.search(ENROLL_RE, q, re.I):
        enroll = _enrollment_for(s)
        system += (f"\n\n2027 OPEN ENROLLMENT ({enroll['window'] or 'none'}), priced against this year's care by the cost "
                   "engine. Comparing plans on cost and networks is part of your job, not medical advice: answer with "
                   "the recommendation below.\n" + enroll["headline"])
        system += "".join(f"\n- {o['name']}{' (current)' if o['current'] else ''}: about ${o['total']:,.0f}/yr "
                          f"(premium ${o['premium']:,.0f} + out-of-pocket ${o['outOfPocket']:,.0f}"
                          + (f" - employer HSA ${o['employerHsa']:,.0f}" if o['employerHsa'] else "") + ")"
                          + (f", out of network: {', '.join(o['outOfNetworkForYou'])}" if o['outOfNetworkForYou'] else "")
                          for o in enroll.get("options", []))

    excerpts = _doc_excerpts(s["docText"], q)
    if excerpts:
        system += ("\n\nEXCERPTS FROM DOCUMENTS THE MEMBER UPLOADED (may be a real plan; prefer them over the "
                   "plan data when they conflict, and name the document):\n" + excerpts)
    return system


# Identical prompt -> identical answer. Judges tapping the same suggestion chips in the
# same demo state share one Bedrock call.
_reply_cache: OrderedDict[str, str] = OrderedDict()
REPLY_CACHE_SIZE = 500
CACHE_STATS = {"hits": 0, "misses": 0}


def _cache_key(system: str, history: list[dict], q: str) -> str:
    norm_q = re.sub(r"[^a-z0-9 ]", "", q.lower()).strip()
    return hashlib.sha256(json.dumps([system, history, norm_q]).encode()).hexdigest()




def _canned(s: dict, q: str) -> str:
    """Answer without the LLM: straight from the cost engine, or the sample script."""
    for pattern, service_id in FALLBACK_SERVICES:
        if re.search(pattern, q, re.I):
            e = _estimate(s, service_id)
            best = e["cheapest"]
            lines = [f"**{_money(best['youPay'])}** for a {e['service'].lower()} at {best['provider']}."]
            lines += [f"- {b['label']}: {_money(b['amount'])}" for b in best["breakdown"]]
            if e["savingsVsMostExpensive"] > 0:
                worst = e["options"][-1]
                lines.append(f"- {worst['provider']} would be {_money(worst['youPay'])}.")
            if e["requiresPriorAuth"]:
                lines.append("- This needs prior authorization first.")
            if service_id == "er_visit":
                lines.append("- If it's an emergency, go to the ER or call 911.")
            return "\n".join(lines)
    if s["insurance"]["source"] == "sample":
        for pattern, answer in D.CANNED_ANSWERS:
            if re.search(pattern, q, re.I):
                return answer
    return D.CANNED_DEFAULT


def _plain(text: str) -> str:
    """Keep replies to the **bold** + "- " bullet format the UI renders."""
    text = text.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-").replace("\u202f", " ")
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+)(?<!\*)\*(?!\*)", r"\1", text)  # *italics* -> plain
    text = re.sub(r"^\s*[•*]\s+", "- ", text, flags=re.M)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return re.sub(r"[ \t]+$", "", text, flags=re.M).strip()


class Ask(BaseModel):
    message: str


@app.get("/api/chat")
def chat_history(x_session_id: str | None = Header(None)):
    s = session(x_session_id)
    return {"messages": s["chat"], "suggestions": D.CHAT_SUGGESTIONS}


@app.post("/api/chat")
def chat(body: Ask, x_session_id: str | None = Header(None)):
    """Reply text uses **bold** and "- " bullets (same format the demo's mdlite() renders)."""
    q = body.message.strip()[:1000]
    if not q:
        raise HTTPException(400, "Empty message")
    s = session(x_session_id)
    history = s["chat"][-4:]
    system = _chat_system(s, q)
    key = _cache_key(system, history, q)
    if key in _reply_cache:
        _reply_cache.move_to_end(key)
        CACHE_STATS["hits"] += 1
        reply, source = _reply_cache[key], "cache"
    else:
        CACHE_STATS["misses"] += 1
        reply = llm.complete(system, history + [{"role": "user", "text": q}], max_tokens=700, effort="low", purpose="chat",
                             deadline=12)
        reply = _plain(reply) if reply else None
        source = "bedrock"
        if reply:
            _reply_cache[key] = reply
            if len(_reply_cache) > REPLY_CACHE_SIZE:
                _reply_cache.popitem(last=False)
        else:
            reply, source = _canned(s, q), "fallback"
    s["chat"] += [{"role": "user", "text": q}, {"role": "assistant", "text": reply}]
    return {"reply": reply, "source": source}


# ---------- 7. year in care ----------

@app.get("/api/recap")
def recap(x_session_id: str | None = Header(None)):
    return _t(D.RECAP, session(x_session_id))
