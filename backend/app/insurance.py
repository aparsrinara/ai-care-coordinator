"""The member's insurance: typed in, read from their plan documents, or the sample card.

Where each benefit number came from is tracked per field, so the UI and the chat
can say "from your Summary of Benefits" vs "typical for a PPO, upload your plan
documents for exact numbers". In production, benefits and accumulators come from
an X12 270/271 eligibility check through a clearinghouse, or the plan's Patient
Access API; the member only types (or scans) payer + member ID.
"""
import hashlib
import json
import re

from . import costs, llm

BENEFIT_FIELDS = {
    "deductible": "Individual in-network deductible ($)",
    "oopMax": "Individual in-network out-of-pocket max ($)",
    "coinsurance": "In-network coinsurance after deductible (0-1, e.g. 0.2)",
    "oonCoinsurance": "Out-of-network coinsurance (0-1; 1 = not covered)",
    "primaryCareCopay": "Primary care visit copay ($)",
    "specialistCopay": "Specialist visit copay ($)",
    "urgentCareCopay": "Urgent care copay ($)",
    "emergencyCopay": "Emergency room copay ($)",
    "physicalTherapyCopay": "Physical therapy visit copay ($)",
    "genericRx90Copay": "Generic drug, 90-day supply at a preferred pharmacy ($)",
    "genericRx90NonPreferred": "Generic drug, 90-day supply at a non-preferred pharmacy ($)",
    "deductibleMet": "Deductible met so far this year ($)",
    "oopMet": "Out-of-pocket spending so far this year ($)",
}

PLAN_TYPES = ["PPO", "HMO", "EPO", "POS", "HDHP", "Medicare Advantage", "Medicaid"]

# Typical 2026 in-network benefits by plan type, used for anything the member didn't provide.
TYPICAL = {
    "PPO": {"deductible": 1500, "oopMax": 4000, "coinsurance": 0.2, "oonCoinsurance": 0.4, "primaryCareCopay": 25,
            "specialistCopay": 50, "urgentCareCopay": 75, "emergencyCopay": 350, "physicalTherapyCopay": 40,
            "genericRx90Copay": 20, "genericRx90NonPreferred": 58},
    "POS": {"deductible": 1000, "oopMax": 4000, "coinsurance": 0.2, "oonCoinsurance": 0.4, "primaryCareCopay": 25,
            "specialistCopay": 45, "urgentCareCopay": 60, "emergencyCopay": 300, "physicalTherapyCopay": 40,
            "genericRx90Copay": 20, "genericRx90NonPreferred": 50},
    "EPO": {"deductible": 1000, "oopMax": 4500, "coinsurance": 0.2, "oonCoinsurance": 1.0, "primaryCareCopay": 25,
            "specialistCopay": 50, "urgentCareCopay": 60, "emergencyCopay": 300, "physicalTherapyCopay": 40,
            "genericRx90Copay": 20, "genericRx90NonPreferred": 50},
    "HMO": {"deductible": 500, "oopMax": 3000, "coinsurance": 0.1, "oonCoinsurance": 1.0, "primaryCareCopay": 15,
            "specialistCopay": 35, "urgentCareCopay": 50, "emergencyCopay": 250, "physicalTherapyCopay": 30,
            "genericRx90Copay": 10, "genericRx90NonPreferred": 40},
    "HDHP": {"deductible": 3200, "oopMax": 6500, "coinsurance": 0.2, "oonCoinsurance": 0.5, "primaryCareCopay": None,
             "specialistCopay": None, "urgentCareCopay": None, "emergencyCopay": None, "physicalTherapyCopay": None,
             "genericRx90Copay": None, "genericRx90NonPreferred": None},
    "Medicare Advantage": {"deductible": 0, "oopMax": 5300, "coinsurance": 0.2, "oonCoinsurance": 1.0, "primaryCareCopay": 0,
                           "specialistCopay": 40, "urgentCareCopay": 45, "emergencyCopay": 110, "physicalTherapyCopay": 30,
                           "genericRx90Copay": 0, "genericRx90NonPreferred": 30},
    "Medicaid": {"deductible": 0, "oopMax": 0, "coinsurance": 0, "oonCoinsurance": 1.0, "primaryCareCopay": 0,
                 "specialistCopay": 0, "urgentCareCopay": 0, "emergencyCopay": 0, "physicalTherapyCopay": 0,
                 "genericRx90Copay": 0, "genericRx90NonPreferred": 0},
}
TYPICAL_PREMIUM_YEAR = {"PPO": 3480, "POS": 3100, "EPO": 2900, "HMO": 2640, "HDHP": 1560,
                        "Medicare Advantage": 0, "Medicaid": 0}

ENROLLMENT_WINDOWS = {
    "employer": "Your employer's open enrollment, usually in November",
    "marketplace": "Nov 1, 2026 - Jan 15, 2027 on HealthCare.gov or your state marketplace",
    "medicare": "Oct 15 - Dec 7, 2026 (Medicare Annual Enrollment)",
    "medicaid": None,
}

# Common US payers for the plan search box. Members can type any name.
PAYER_DIRECTORY = [
    "Aetna", "Anthem Blue Cross", "Blue Cross Blue Shield", "Cigna", "Humana", "Kaiser Permanente",
    "UnitedHealthcare", "Oscar Health", "Molina Healthcare", "Centene / Ambetter", "Highmark",
    "Florida Blue", "Blue Shield of California", "Medicare", "Medicaid", "Blue Ridge Health (sample)",
]


def sample() -> dict:
    """Maya's sample card and benefits (the demo's default)."""
    return {
        "source": "sample",
        "payer": "Blue Ridge Health", "planName": "Blue Ridge PPO Silver (2026)", "planType": "PPO",
        "coverageSource": "employer", "memberName": "Maya Chen", "memberId": "BRX 482 190 337",
        "groupNumber": "20418", "rxBin": "610014", "coverage": "Individual",
        "premiumYear": 3480,
        "benefits": dict(TYPICAL["PPO"], deductibleMet=1120, oopMet=2340),
        "benefitSources": {k: "sample" for k in BENEFIT_FIELDS},
        "documents": [],
    }


def _num(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    n = float(m.group(0).replace(",", ""))
    return n / 100 if "%" in str(v) else n


def _clean_benefits(raw: dict) -> dict:
    out = {}
    for k in BENEFIT_FIELDS:
        n = _num(raw.get(k))
        if n is None:
            continue
        if k in ("coinsurance", "oonCoinsurance") and n > 1:
            n = n / 100  # "20" -> 0.2
        out[k] = n
    return out


def normalize_type(plan_type: str | None, plan_name: str = "") -> str:
    text = f"{plan_type or ''} {plan_name}".lower()
    if "medicaid" in text:
        return "Medicaid"
    if "medicare" in text:
        return "Medicare Advantage"
    if any(w in text for w in ("hdhp", "hsa", "high deductible", "bronze")):
        return "HDHP"
    for t in ("EPO", "POS", "HMO", "PPO"):
        if t.lower() in text:
            return t
    return "PPO"


def from_entry(entry: dict) -> dict:
    """Build the member's insurance from what they typed. Missing benefits use typical values for the plan type."""
    plan_type = normalize_type(entry.get("planType"), entry.get("planName", ""))
    entered = _clean_benefits(entry.get("benefits") or {})
    benefits = dict(TYPICAL[plan_type], deductibleMet=0, oopMet=0)
    benefits.update(entered)
    sources = {k: "entered" if k in entered else ("assumed" if k in ("deductibleMet", "oopMet") else "typical")
               for k in BENEFIT_FIELDS}
    payer = (entry.get("payer") or "Your health plan").strip()
    coverage_source = entry.get("coverageSource") or (
        "medicare" if plan_type == "Medicare Advantage" else "medicaid" if plan_type == "Medicaid" else "employer")
    premium = _num(entry.get("premiumMonthly"))
    return {
        "source": "entered",
        "payer": payer,
        "planName": (entry.get("planName") or f"{payer} {plan_type}").strip(),
        "planType": plan_type,
        "coverageSource": coverage_source,
        "memberName": (entry.get("memberName") or "").strip(),
        "memberId": (entry.get("memberId") or "").strip(),
        "groupNumber": (entry.get("groupNumber") or "").strip(),
        "rxBin": (entry.get("rxBin") or "").strip(),
        "coverage": entry.get("coverage") or "Just you",
        "premiumYear": premium * 12 if premium is not None else TYPICAL_PREMIUM_YEAR[plan_type],
        "premiumSource": "entered" if premium is not None else "typical",
        "benefits": benefits,
        "benefitSources": sources,
        "documents": [],
    }


def apply_document(ins: dict, extracted: dict, doc_name: str) -> list[str]:
    """Merge benefits read from a plan document. Returns the fields that were updated."""
    found = _clean_benefits(extracted)
    for k, v in found.items():
        if ins["benefitSources"].get(k) == "entered":
            continue  # what the member typed wins
        ins["benefits"][k] = v
        ins["benefitSources"][k] = "plan_document"
    for k in ("payer", "planName"):
        if extracted.get(k) and ins["source"] != "sample" and not ins.get(k):
            ins[k] = extracted[k]
    if extracted.get("planType") and ins["source"] == "entered":
        ins["planType"] = normalize_type(extracted["planType"], ins["planName"])
    ins["documents"].append(doc_name)
    return sorted(found)


EXTRACT_SYSTEM = """You read a US health plan document (usually a Summary of Benefits and Coverage) and extract in-network benefits for ONE person.
Return only JSON, no code fences, with any of these keys you can find (omit what isn't stated; never guess):
{"payer": str, "planName": str, "planType": "PPO|HMO|EPO|POS|HDHP|Medicare Advantage|Medicaid",
 "deductible": number, "oopMax": number, "coinsurance": number 0-1, "oonCoinsurance": number 0-1 (1 if not covered),
 "primaryCareCopay": number, "specialistCopay": number, "urgentCareCopay": number, "emergencyCopay": number,
 "physicalTherapyCopay": number, "genericRx90Copay": number, "genericRx90NonPreferred": number}
Use the individual (not family) amounts. If a service says "deductible then X% coinsurance" instead of a copay, omit that copay key.
A 30-day generic copay times 3 is fine for the 90-day fields only if no 90-day amount is given."""


_extract_cache: dict[str, dict] = {}


def extract_benefits(text: str) -> dict | None:
    """Cached by document hash: the same SBC uploaded twice costs one Bedrock call."""
    key = hashlib.sha256(text[:24000].encode()).hexdigest()
    if key in _extract_cache:
        return dict(_extract_cache[key])
    result = _extract(text)
    if result:
        _extract_cache[key] = result
    return dict(result) if result else None


def _extract(text: str) -> dict | None:
    out = llm.complete(EXTRACT_SYSTEM, [{"role": "user", "text": text[:24000]}], max_tokens=2000, purpose="benefits_extraction")
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ---------- views used by the API ----------

def engine(ins: dict) -> dict:
    return costs.engine_plan(ins["benefits"], ins["planName"], ins["planType"], premium=ins["premiumYear"])


def alternatives(ins: dict) -> dict[str, dict]:
    """Next year's options: the current plan plus the payer's other common plan types."""
    plans = {"current": engine(ins)}
    for t in ("HMO", "HDHP", "PPO"):
        if t == ins["planType"]:
            continue
        b = dict(TYPICAL[t])
        plans[t.lower()] = costs.engine_plan(
            b, f"{ins['payer']} {t}", t, premium=TYPICAL_PREMIUM_YEAR[t],
            employer_hsa=1000 if t == "HDHP" and ins["coverageSource"] == "employer" else 0,
            # Narrow network: the dummy record's dermatologist and hospital aren't in it.
            out_of_network={"dr-lee", "st-marys"} if t == "HMO" else (), referrals_required=t == "HMO")
    return plans


def display(ins: dict) -> dict:
    """Plan facts for the chat prompt and the coverage screen."""
    b = ins["benefits"]
    src = ins["benefitSources"]
    estimated = sorted(k for k, v in src.items() if v in ("typical", "assumed"))
    return {
        "payer": ins["payer"], "plan": ins["planName"], "planType": ins["planType"],
        "memberId": ins["memberId"], "coverage": ins["coverage"],
        "deductible": {"amount": b["deductible"], "met": b["deductibleMet"]},
        "outOfPocketMax": {"amount": b["oopMax"], "met": b["oopMet"]},
        "coinsuranceAfterDeductible": f"{round(b['coinsurance'] * 100)}% in-network",
        "copays": {k: b[k] for k in ("primaryCareCopay", "specialistCopay", "urgentCareCopay", "emergencyCopay",
                                     "physicalTherapyCopay", "genericRx90Copay", "genericRx90NonPreferred")},
        "preventiveCare": "$0 in-network (annual physical, screenings, vaccines)",
        "fsa": {"balanceLeft": 640, "deadline": "Dec 31, 2026, no rollover"} if ins["source"] == "sample" else None,
        "estimatedFields": estimated,
        "planDocuments": ins["documents"],
    }
