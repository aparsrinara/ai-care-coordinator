"""Cost engine: what the member pays, computed from plan rules instead of by the LLM.

A plan (see engine_plan) has one rule per service category:
  {"type": "free"}                                  preventive care
  {"type": "copay", "amount": 40}                   flat copay, deductible doesn't apply
  {"type": "deductible_coinsurance"}                deductible first, then plan coinsurance
  {"type": "deductible_then_copay", "amount": 350}  deductible first, then a flat copay
  {"type": "pharmacy"}                              preferred / non-preferred pharmacy copay
Out-of-network providers use deductible + out-of-network coinsurance.
All amounts are capped by what's left of the out-of-pocket max.
"""
from dataclasses import dataclass

# Allowed (negotiated) prices. In production: the plan's Transparency in Coverage data.
# For a plan the member typed in, these are typical in-network prices, flagged as estimates.
SERVICES = {
    "annual_physical": {"name": "Annual physical", "category": "preventive", "options": [("Northside Health · Dr. Nair", "northside", 310)]},
    "primary_care": {"name": "Primary care visit", "category": "primary_care", "options": [("Northside Health · Dr. Nair", "northside", 180)]},
    "specialist": {"name": "Specialist visit", "category": "specialist", "options": [("In-network specialist", "any", 260)]},
    "derm_new_visit": {"name": "Dermatology new-patient visit", "category": "specialist", "options": [("Dr. Lee Dermatology", "dr-lee", 260)]},
    "pt_visit": {"name": "Physical therapy visit", "category": "physical_therapy", "options": [("Motion PT", "motion-pt", 140)]},
    "lab_panel": {"name": "Lab panel (lipids + CMP)", "category": "lab", "options": [("Northside Lab", "northside", 185)]},
    "knee_mri": {"name": "Knee MRI", "category": "imaging", "requiresPriorAuth": True, "options": [
        ("Clearview Imaging (imaging center)", "clearview", 1200),
        ("St. Mary's Hospital (hospital outpatient)", "st-marys", 2900)]},
    "urgent_care": {"name": "Urgent care visit", "category": "urgent_care", "options": [("CityCare Urgent Care", "any", 220)]},
    "er_visit": {"name": "Emergency room visit", "category": "emergency", "options": [("St. Mary's Hospital ER", "st-marys", 2400)]},
    "generic_90day": {"name": "Generic drug, 90-day supply", "category": "generic_rx", "options": [
        ("Elm St Pharmacy (preferred)", "preferred", 60), ("Mail order (preferred)", "preferred", 60),
        ("Non-preferred pharmacy", "non-preferred", 60)]},
}
CATEGORIES = ("preventive", "primary_care", "specialist", "physical_therapy", "lab", "imaging",
              "urgent_care", "emergency", "generic_rx")

# Maya's care this year, from her claims. Used to project next year's costs.
UTILIZATION_2026 = [
    ("annual_physical", 1), ("primary_care", 2), ("specialist", 2), ("derm_new_visit", 1),
    ("pt_visit", 12), ("lab_panel", 2), ("urgent_care", 1), ("generic_90day", 4),
]


def engine_plan(b: dict, name: str, plan_type: str, out_of_network=(), premium: float = 0,
                employer_hsa: float = 0, referrals_required: bool = False) -> dict:
    """Turn a benefits dict (see insurance.BENEFIT_FIELDS) into engine rules."""
    hdhp = plan_type == "HDHP"

    def copay_or_ded(key):
        return {"type": "deductible_coinsurance"} if hdhp or b.get(key) is None else {"type": "copay", "amount": b[key]}

    rules = {
        "preventive": {"type": "free"},
        "primary_care": copay_or_ded("primaryCareCopay"),
        "specialist": copay_or_ded("specialistCopay"),
        "physical_therapy": copay_or_ded("physicalTherapyCopay"),
        "lab": {"type": "deductible_coinsurance"},
        "imaging": {"type": "deductible_coinsurance"},
        "urgent_care": copay_or_ded("urgentCareCopay"),
        "emergency": ({"type": "deductible_coinsurance"} if hdhp or b.get("emergencyCopay") is None
                      else {"type": "deductible_then_copay", "amount": b["emergencyCopay"]}),
        "generic_rx": {"type": "deductible_coinsurance"} if hdhp else {"type": "pharmacy"},
    }
    return {
        "name": name, "type": plan_type, "deductible": b["deductible"], "oopMax": b["oopMax"],
        "coinsurance": b["coinsurance"], "oonCoinsurance": b.get("oonCoinsurance", 0.4),
        "rx": {"preferred": b.get("genericRx90Copay", 20), "non-preferred": b.get("genericRx90NonPreferred", 58)},
        "outOfNetwork": set(out_of_network), "rules": rules, "premiumYear": premium,
        "employerHsa": employer_hsa, "referralsRequired": referrals_required,
    }


@dataclass
class Tracker:
    """Running deductible / out-of-pocket totals for one member-year."""
    plan: dict
    deductible_met: float = 0
    oop_met: float = 0
    total_paid: float = 0  # includes out-of-network costs that don't count toward the OOP max

    def charge(self, price: float, category: str, provider_id: str) -> dict:
        p = self.plan
        rule = p["rules"][category]
        ded_left = max(0, p["deductible"] - self.deductible_met)
        oop_left = max(0, p["oopMax"] - self.oop_met)
        steps = []
        if provider_id in p["outOfNetwork"]:
            if p["oonCoinsurance"] >= 1:
                steps.append(("Out of network: not covered", price))
                return self._finish(price, 0, steps, oop_left, counts=False, in_network=False)
            rule = {"type": "deductible_coinsurance", "coinsurance": p["oonCoinsurance"]}
        kind = rule["type"]
        if kind == "free":
            steps.append(("Preventive care, covered in full", 0))
            return self._finish(0, 0, steps, oop_left)
        if kind == "copay":
            steps.append((f"${rule['amount']:g} copay, deductible doesn't apply", rule["amount"]))
            return self._finish(min(rule["amount"], price), 0, steps, oop_left)
        if kind == "pharmacy":
            copay = p["rx"]["non-preferred" if provider_id == "non-preferred" else "preferred"]
            steps.append(("Pharmacy copay for this supply", copay))
            return self._finish(copay, 0, steps, oop_left)
        to_ded = min(price, ded_left)
        if to_ded:
            steps.append(("Rest of your deductible" if self.deductible_met else "Deductible", to_ded))
        remaining = price - to_ded
        if kind == "deductible_then_copay":
            after = min(rule["amount"], remaining)
            if after:
                steps.append((f"${rule['amount']:g} copay after deductible", after))
        else:
            rate = rule.get("coinsurance", p["coinsurance"])
            after = round(remaining * rate, 2)
            if remaining:
                steps.append((f"{round(rate * 100)}% coinsurance on ${remaining:,.0f}", after))
        return self._finish(to_ded + after, to_ded, steps, oop_left)

    def _finish(self, owed, to_ded, steps, oop_left, counts=True, in_network=True):
        capped = min(owed, oop_left) if counts else owed
        if capped < owed:
            steps.append(("Capped: you hit your out-of-pocket max", capped - owed))
        self.total_paid += capped
        if counts:
            self.oop_met += capped
            self.deductible_met += to_ded
        return {"youPay": round(capped, 2), "breakdown": [{"label": l, "amount": round(a, 2)} for l, a in steps],
                "inNetwork": in_network}


def estimate(service_id: str, plan: dict, deductible_met: float, oop_met: float) -> dict:
    """What one service costs the member right now, for every place they could get it."""
    svc = SERVICES[service_id]
    options = []
    for label, provider_id, price in svc["options"]:
        t = Tracker(plan, deductible_met, oop_met)
        options.append({"provider": label, "negotiatedPrice": price, **t.charge(price, svc["category"], provider_id)})
    options.sort(key=lambda o: o["youPay"])
    return {
        "service": svc["name"], "serviceId": service_id, "plan": plan["name"],
        "requiresPriorAuth": svc.get("requiresPriorAuth", False),
        "deductibleLeft": max(0, plan["deductible"] - deductible_met),
        "oopLeft": max(0, plan["oopMax"] - oop_met),
        "options": options,
        "cheapest": options[0],
        "savingsVsMostExpensive": round(options[-1]["youPay"] - options[0]["youPay"], 2),
    }


def all_estimates(plan: dict, deductible_met: float, oop_met: float) -> list[dict]:
    return [estimate(sid, plan, deductible_met, oop_met) for sid in SERVICES if sid != "annual_physical"]


def project_year(plan: dict, utilization=UTILIZATION_2026) -> dict:
    """Total yearly cost (premiums + out-of-pocket - employer HSA money) for a pattern of care."""
    t = Tracker(plan)
    out_of_network = []
    for service_id, count in utilization:
        svc = SERVICES[service_id]
        label, provider_id, price = svc["options"][0]
        if provider_id in plan["outOfNetwork"]:
            out_of_network.append(label)
        for _ in range(count):
            t.charge(price, svc["category"], provider_id)
    oop = round(t.total_paid)
    return {
        "name": plan["name"], "type": plan["type"],
        "premium": plan["premiumYear"], "outOfPocket": oop, "employerHsa": plan["employerHsa"],
        "total": plan["premiumYear"] + oop - plan["employerHsa"],
        "deductible": plan["deductible"], "oopMax": plan["oopMax"],
        "referralsRequired": plan["referralsRequired"],
        "outOfNetworkForYou": sorted(set(out_of_network)),
        "worstCase": plan["premiumYear"] + plan["oopMax"] - plan["employerHsa"],
    }


def compare_plans(plans: dict[str, dict], current_id: str) -> dict:
    options = []
    for pid, plan in plans.items():
        options.append(dict(project_year(plan), planId=pid, current=pid == current_id))
    current = next(o for o in options if o["current"])
    for o in options:
        o["vsCurrent"] = o["total"] - current["total"]
    # Cheapest plan that keeps all of the member's doctors in network.
    best = min((o for o in options if not o["outOfNetworkForYou"]), key=lambda o: o["total"])
    return {"basis": "This year's care: " + ", ".join(f"{n} {SERVICES[s]['name'].lower()}" for s, n in UTILIZATION_2026),
            "options": sorted(options, key=lambda o: o["total"]), "recommended": best["planId"]}
