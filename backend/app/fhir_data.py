"""Sample patient record for the demo, stored as FHIR R4 resources.

Everything here is fake. In production these resources would come from:
- the health plan's CMS Patient Access API (Coverage, ExplanationOfBenefit)
- the health system's SMART on FHIR endpoint, e.g. Epic (Patient, Encounter,
  DocumentReference, ServiceRequest, Appointment, MedicationRequest)
- the pharmacy (MedicationDispense)
The rest of the app only reads from BUNDLE, so swapping in a real FHIR client
means replacing this module.
"""

PATIENT_ID = "maya-chen"

VISIT_NOTE = """\
NORTHSIDE HEALTH - PRIMARY CARE
After Visit Summary / Progress Note
Patient: Chen, Maya   DOB: 03/04/1991   MRN: NH-0048812
Date of service: 09/22/2026   Provider: Priya Nair, MD (Internal Medicine)
Visit type: Annual preventive exam (CPT 99395) + problem-oriented (knee)

SUBJECTIVE
35 y.o. F here for annual physical. Feels well overall. Reports R knee pain x 3 months,
worse with stairs and running, no trauma, no locking/giving way. Tried ibuprofen PRN
with partial relief. Also notes a mole on the L upper back that her spouse thinks has
gotten darker over the past year. Taking atorvastatin 10 mg daily, adherent, no side
effects. Due for 90-day supply.

OBJECTIVE
BP 118/76, HR 68, BMI 23.4. Gen: well appearing.
Skin: 6 mm irregularly pigmented macule L upper back, borders slightly irregular.
R knee: no effusion, full ROM, tenderness at lateral patellar facet, + patellar grind.
Ligaments stable. Lachman negative, McMurray negative.

ASSESSMENT / PLAN
1. Annual exam (Z00.00): Up to date on vaccines. Counseled on exercise, sunscreen.
2. Hyperlipidemia (E78.5): On atorvastatin 10 mg. Last LDL 142 (2025).
   -> Fasting lipid panel + CMP. Refill atorvastatin 10 mg PO daily, #90, RF x3.
3. Patellofemoral pain syndrome, R knee (M22.2X1): Exam c/w PFPS, no signs of
   internal derangement. Imaging not indicated at this time; consider MRI if no
   improvement after 6-8 wks of PT.
   -> Referral to physical therapy, 2x/week x 6 weeks (12 visits).
4. Atypical nevus, L upper back (D22.5): ABCDE: asymmetry, color variation.
   -> Referral to dermatology for evaluation +/- biopsy. Routine priority.

Follow up in 12 months or sooner PRN. Patient verbalized understanding.
Electronically signed: Priya Nair, MD 09/22/2026 16:42
"""


def _ref(kind: str, id_: str) -> dict:
    return {"reference": f"{kind}/{id_}"}


PATIENT = {
    "resourceType": "Patient",
    "id": PATIENT_ID,
    "name": [{"family": "Chen", "given": ["Maya"]}],
    "birthDate": "1991-03-04",
    "gender": "female",
    "telecom": [
        {"system": "email", "value": "maya.chen@example.com"},
        {"system": "phone", "value": "(555) 014-2291", "use": "mobile"},
    ],
}

PRACTITIONERS = [
    {"resourceType": "Practitioner", "id": "dr-nair", "name": [{"family": "Nair", "given": ["Priya"], "prefix": ["Dr."]}],
     "qualification": [{"code": {"text": "Internal Medicine"}}]},
    {"resourceType": "Practitioner", "id": "dr-lee", "name": [{"family": "Lee", "given": ["Jordan"], "prefix": ["Dr."]}],
     "qualification": [{"code": {"text": "Dermatology"}}]},
]

ORGANIZATIONS = [
    {"resourceType": "Organization", "id": "blue-ridge", "name": "Blue Ridge Health", "type": [{"text": "Payer"}]},
    {"resourceType": "Organization", "id": "northside", "name": "Northside Health", "type": [{"text": "Health system"}]},
    {"resourceType": "Organization", "id": "elm-st", "name": "Elm St Pharmacy", "type": [{"text": "Pharmacy"}]},
    {"resourceType": "Organization", "id": "motion-pt", "name": "Motion PT", "type": [{"text": "Physical therapy"}]},
    {"resourceType": "Organization", "id": "clearview", "name": "Clearview Imaging", "type": [{"text": "Imaging center"}]},
]

COVERAGE = {
    "resourceType": "Coverage",
    "id": "cov-2026",
    "status": "active",
    "subscriber": _ref("Patient", PATIENT_ID),
    "beneficiary": _ref("Patient", PATIENT_ID),
    "subscriberId": "BRX482190337",
    "payor": [_ref("Organization", "blue-ridge")],
    "class": [
        {"type": {"text": "group"}, "value": "20418"},
        {"type": {"text": "plan"}, "value": "SILVER-1500", "name": "Blue Ridge PPO Silver (2026)"},
        {"type": {"text": "rxbin"}, "value": "610014"},
    ],
    "period": {"start": "2026-01-01", "end": "2026-12-31"},
}

ENCOUNTER = {
    "resourceType": "Encounter",
    "id": "enc-2026-09-22",
    "status": "finished",
    "class": {"code": "AMB"},
    "type": [{"text": "Annual physical"}],
    "subject": _ref("Patient", PATIENT_ID),
    "participant": [{"individual": _ref("Practitioner", "dr-nair")}],
    "serviceProvider": _ref("Organization", "northside"),
    "period": {"start": "2026-09-22T15:30:00-04:00", "end": "2026-09-22T16:10:00-04:00"},
}

DOCUMENT_REFERENCE = {
    "resourceType": "DocumentReference",
    "id": "note-2026-09-22",
    "status": "current",
    "type": {"coding": [{"system": "http://loinc.org", "code": "11506-3", "display": "Progress note"}]},
    "subject": _ref("Patient", PATIENT_ID),
    "context": {"encounter": [_ref("Encounter", ENCOUNTER["id"])]},
    "content": [{"attachment": {"contentType": "text/plain", "data_text": VISIT_NOTE}}],
}

SERVICE_REQUESTS = [
    {"resourceType": "ServiceRequest", "id": "sr-labs", "status": "active", "intent": "order",
     "code": {"text": "Fasting lipid panel + CMP"}, "encounter": _ref("Encounter", ENCOUNTER["id"])},
    {"resourceType": "ServiceRequest", "id": "sr-derm", "status": "active", "intent": "order",
     "code": {"text": "Referral to dermatology"}, "reasonCode": [{"text": "Atypical nevus, L upper back"}],
     "performer": [_ref("Practitioner", "dr-lee")], "encounter": _ref("Encounter", ENCOUNTER["id"])},
    {"resourceType": "ServiceRequest", "id": "sr-pt", "status": "active", "intent": "order",
     "code": {"text": "Physical therapy, 2x/week x 6 weeks"}, "quantityQuantity": {"value": 12, "unit": "visits"},
     "reasonCode": [{"text": "Patellofemoral pain syndrome, R knee"}],
     "performer": [_ref("Organization", "motion-pt")], "encounter": _ref("Encounter", ENCOUNTER["id"])},
]

MEDICATION_REQUEST = {
    "resourceType": "MedicationRequest", "id": "mr-atorva", "status": "active", "intent": "order",
    "medicationCodeableConcept": {"text": "Atorvastatin 10 mg tablet"},
    "dosageInstruction": [{"text": "1 tablet by mouth daily"}],
    # The e-prescription names the patient's pharmacy of choice, so the portal tells us where it went.
    "dispenseRequest": {"quantity": {"value": 90, "unit": "tablets"}, "numberOfRepeatsAllowed": 3,
                        "performer": _ref("Organization", "elm-st")},
    "encounter": _ref("Encounter", ENCOUNTER["id"]),
}

APPOINTMENTS = [
    {"resourceType": "Appointment", "id": "appt-labs", "status": "booked", "description": "Fasting blood work",
     "start": "2026-09-28T07:40:00-04:00", "basedOn": [_ref("ServiceRequest", "sr-labs")],
     "participant": [{"actor": {"display": "Northside Lab"}}]},
    {"resourceType": "Appointment", "id": "appt-derm", "status": "booked", "description": "Dermatology skin check",
     "start": "2026-10-07T08:15:00-04:00", "basedOn": [_ref("ServiceRequest", "sr-derm")],
     "participant": [{"actor": _ref("Practitioner", "dr-lee")}]},
]

# Aug 14 lab visit, billed twice: the "duplicate charge" the coverage screen flags.
EXPLANATIONS_OF_BENEFIT = [
    {"resourceType": "ExplanationOfBenefit", "id": f"eob-{n}", "status": "active", "use": "claim",
     "patient": _ref("Patient", PATIENT_ID), "insurer": _ref("Organization", "blue-ridge"),
     "provider": {"display": "Northside Lab"}, "billablePeriod": {"start": "2026-08-14"},
     "item": [{"productOrService": {"text": "Lab panel"}, "net": {"value": 185, "currency": "USD"}}],
     "total": [{"category": {"text": "member liability"}, "amount": {"value": 185, "currency": "USD"}}]}
    for n in ("0814-a", "0814-b")
]

# Results of the Sep 28 fasting labs. They arrive when the demo "fast-forwards"
# (POST /api/demo/results-in). Prior LDL is from 2025 for the trend.
LAB_DATE = "2026-09-28T07:40:00-04:00"


def _obs(id_: str, loinc: str, name: str, value: float, unit: str, low=None, high=None, date=LAB_DATE, flag=None):
    rng = {}
    if low is not None:
        rng["low"] = {"value": low, "unit": unit}
    if high is not None:
        rng["high"] = {"value": high, "unit": unit}
    obs = {"resourceType": "Observation", "id": id_, "status": "final",
           "category": [{"coding": [{"code": "laboratory"}]}],
           "code": {"coding": [{"system": "http://loinc.org", "code": loinc, "display": name}], "text": name},
           "subject": _ref("Patient", PATIENT_ID), "effectiveDateTime": date,
           "valueQuantity": {"value": value, "unit": unit}, "referenceRange": [rng]}
    if flag:
        obs["interpretation"] = [{"coding": [{"code": flag}], "text": {"H": "High", "L": "Low", "N": "Normal"}[flag]}]
    return obs


LAB_OBSERVATIONS = [
    _obs("obs-ldl", "13457-7", "LDL cholesterol", 118, "mg/dL", high=100, flag="H"),
    _obs("obs-hdl", "2085-9", "HDL cholesterol", 58, "mg/dL", low=40, flag="N"),
    _obs("obs-tg", "2571-8", "Triglycerides", 102, "mg/dL", high=150, flag="N"),
    _obs("obs-chol", "2093-3", "Total cholesterol", 196, "mg/dL", high=200, flag="N"),
    _obs("obs-glu", "2345-7", "Glucose, fasting", 91, "mg/dL", low=70, high=99, flag="N"),
    _obs("obs-alt", "1742-6", "ALT (liver)", 24, "U/L", low=7, high=35, flag="N"),
]
PRIOR_LDL = _obs("obs-ldl-2025", "13457-7", "LDL cholesterol", 142, "mg/dL", high=100, date="2025-09-18T08:00:00-04:00", flag="H")

DIAGNOSTIC_REPORT = {
    "resourceType": "DiagnosticReport", "id": "dr-lipids-2026-09-28", "status": "final",
    "code": {"text": "Lipid panel + comprehensive metabolic panel"},
    "subject": _ref("Patient", PATIENT_ID), "basedOn": [_ref("ServiceRequest", "sr-labs")],
    "effectiveDateTime": LAB_DATE, "issued": "2026-09-29T14:05:00-04:00",
    "performer": [{"display": "Northside Lab"}],
    "result": [_ref("Observation", o["id"]) for o in LAB_OBSERVATIONS],
    # The ordering doctor's released comment (what the patient portal shows).
    "conclusion": "LDL improved from 142 to 118 on atorvastatin, liver tests normal. Continue atorvastatin 10 mg. "
                  "Keep up the exercise. Recheck lipids in 12 months. - Dr. Nair",
}

BUNDLE = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [{"resource": r} for r in [
        PATIENT, *PRACTITIONERS, *ORGANIZATIONS, COVERAGE, ENCOUNTER, DOCUMENT_REFERENCE,
        *SERVICE_REQUESTS, MEDICATION_REQUEST, *APPOINTMENTS, *EXPLANATIONS_OF_BENEFIT, PRIOR_LDL,
    ]],
}
