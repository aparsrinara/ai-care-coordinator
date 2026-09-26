"""Demo content that isn't FHIR: plan benefits, the booking call script, the recap.

The demo's "today" is Friday Sep 25, 2026, three days after the physical.
Text can contain {tokens} (name, payer, prices) that main._t() fills in per member.
"""

DEMO_TODAY = "2026-09-25"
DEMO_TODAY_LABEL = "Friday, Sep 25"

# What each connection is, what it can read, and how it connects in real life.
CONNECTIONS = {
    "plan": {
        "org": "{payer}", "initials": "{payer_initials}", "color": "#1E4C8A",
        "scopes": ["Your coverage, benefits and costs", "Claims and Explanations of Benefits",
                   "Deductible and out-of-pocket totals", "Prior authorization status"],
        "via": "Shared through {payer}'s patient access API (FHIR), using your {payer} member login.",
        "standard": "CMS Patient Access API (CARIN Blue Button)",
    },
    "portal": {
        "org": "Northside Health", "initials": "NH", "color": "#1F5A32",
        "scopes": ["Visit notes and after-visit summaries", "Medications and allergies",
                   "Lab results", "Referrals and upcoming appointments"],
        "via": "Shared through Northside Health's patient access API (SMART on FHIR), the standard way health systems connect patient apps.",
        "standard": "SMART on FHIR (ONC certified API, USCDI)",
    },
}

# Doctors and pharmacies found in the last 12 months of claims (ExplanationOfBenefit).
FOUND_FROM_CLAIMS = [
    {"id": "portal", "kind": "health_system", "name": "Northside Health", "detail": "6 visits · Dr. Priya Nair, primary care",
     "what": "Patient portal: visit notes, labs, referrals", "linkable": True, "required": True},
    {"id": "pharmacy", "kind": "pharmacy", "name": "Main St Drug", "detail": "4 prescription fills",
     "what": "Tracked through your pharmacy claims. No login needed", "linkable": False},
    {"id": "clearview", "kind": "imaging", "name": "Clearview Imaging", "detail": "1 visit · no portal to link",
     "what": "Tracked through your claims", "linkable": False},
]

# Status of each next step from the visit. `channel` is how the coordinator
# actually got it done: patient-facing FHIR APIs are read-only, so actions go
# through phone calls, portal messages or the pharmacy.
NEXT_STEPS = {
    "labs": {"title": "Fasting labs", "status": "booked", "channel": "phone_call",
             "detail": "Mon 9/28, 7:40 AM at Northside Lab. I'll remind you to stop eating after 9 PM Sunday."},
    "refill": {"title": "90-day refill", "status": "ordered", "channel": "pharmacy",
               "detail": "Moved it from Main St Drug to Elm St Pharmacy, which is in your plan's preferred network: {rx_pref} instead of {rx_non}. Ready Saturday."},
    "derm": {"title": "Dermatology referral", "status": "booked", "channel": "phone_call",
             "detail": "Confirmed Dr. Lee's office has Dr. Nair's referral notes, then booked Oct 7. I grabbed a cancellation, 5 weeks sooner.",
             "callId": "derm"},
}

PT_STEPS = [
    {"title": "Asked Dr. Nair's office to submit the request", "detail": "Sent through your Northside portal messages"},
    {"title": "Office submitted it to {payer}", "detail": "Request #PA-40718"},
    {"title": "{payer} approved 12 visits", "detail": "Valid through Mar 31, 2027"},
    {"title": "Booked your first visit at Motion PT", "detail": "Tue Oct 6, 5:30 PM · 1.2 mi away"},
]
PT_STEP_SECONDS = 2.2  # sped up for the demo; real prior auths take 2-10 business days

UPCOMING = [
    {"dow": "SAT", "day": "26", "title": "90-day refill ready", "detail": "Elm St Pharmacy · saved {rx_saved}", "status": "Ordered", "tone": "info", "date": "2026-09-26"},
    {"dow": "MON", "day": "28", "title": "Fasting blood work", "detail": "7:40 AM · Northside Lab", "status": "Booked", "tone": "ok", "date": "2026-09-28"},
    {"dow": "WED", "day": "7", "title": "Dermatology skin check", "detail": "Oct 7, 8:15 AM · Dr. Lee", "status": "Booked", "tone": "ok", "date": "2026-10-07"},
]
PT_UPCOMING = {"dow": "TUE", "day": "6", "title": "Physical therapy, visit 1", "detail": "Oct 6, 5:30 PM · Motion PT",
               "status": "Booked", "tone": "ok", "date": "2026-10-06"}

CALL = {
    "id": "derm",
    "with": "Dr. Lee Dermatology",
    "result": {"status": "booked", "title": "Wed Oct 7, 8:15 AM · Dr. Lee",
               "detail": "5 weeks sooner than the first opening. Added to your calendar, with a reminder the day before."},
    "lines": [
        {"speaker": "coordinator", "text": "Hi, this is an AI care coordinator calling on behalf of {name}, date of birth {dob_spoken}. They've authorized me to schedule for her. I'm checking that you received her dermatology referral from Dr. Priya Nair."},
        {"speaker": "front_desk", "text": "Let me look. Yes, we have it."},
        {"speaker": "coordinator", "text": "Great. What's the soonest new-patient opening with Dr. Lee? They're on a {payer} {plan_type} plan."},
        {"speaker": "front_desk", "text": "First opening is November 11 at 2:30."},
        {"speaker": "coordinator", "text": "Please book that, and add them to your cancellation list. They can come in on short notice on weekday mornings."},
        {"speaker": "front_desk", "text": "Oh, someone just cancelled October 7 at 8:15. Want that instead?"},
        {"speaker": "coordinator", "text": "Yes. {first} already approved weekday mornings. Please book October 7 at 8:15."},
        {"speaker": "front_desk", "text": "Done. {first} is confirmed for October 7 at 8:15 with Dr. Lee."},
    ],
}

DEFAULT_DOCS = [
    {"name": "Summary of Benefits", "meta": "PDF · 42 pages · read", "icon": "doc"},
    {"name": "Insurance card", "meta": "Front + back · photo", "icon": "card"},
    {"name": "Explanation of Benefits", "meta": "Aug 2026 · 1 issue found", "icon": "doc"},
]

RECAP = {
    "year": 2026,
    "intro": {"title": "{first}, your Year in Care is here.", "subtitle": "You showed up for yourself 14 times this year. Here's what that looked like."},
    "numbers": {"appointmentsKept": 14, "refillsOnTime": 11, "refillsTotal": 11, "hoursSaved": 31},
    "money": {
        "total": 2860,
        "breakdown": [
            {"label": "Sent to in-network care", "amount": 1650},
            {"label": "Price-shopped prescriptions", "amount": 610},
            {"label": "FSA dollars used, not lost", "amount": 415},
            {"label": "Billing errors caught", "amount": 185},
        ],
        "biggestWin": "Sending you to in-network specialists instead of out-of-network ones saved $1,650.",
    },
    "scorecard": {
        "score": 86, "change": 12,
        "rows": [
            {"label": "Followed through on next steps", "value": "96%", "grade": "A"},
            {"label": "Refills on time", "value": "11/11", "grade": "A+"},
            {"label": "Preventive care done", "value": "3 of 4", "grade": "B+"},
            {"label": "Benefits used", "value": "72%", "grade": "B"},
        ],
    },
    "nextYear": "Your physical and eye exam are both covered. I'll book them in January, plus the dental cleaning you skipped.",
    "shareText": "My 2026 Year in Care: 14 appointments kept, 11 of 11 refills on time, $2,860 kept in my pocket. Care score 86.",
}

CHAT_SUGGESTIONS = [
    "What will my PT cost?",
    "How do I get the most out of my benefits?",
    "Is urgent care covered?",
    "How do I use my FSA before it expires?",
    "Cheapest place for my refill?",
]

# Used when Bedrock is unreachable, so the demo never dead-ends on stage.
CANNED_ANSWERS = [
    (r"mri|imaging|scan", "About **$544** at Clearview Imaging (in-network).\n- Negotiated price: $1,200\n- Rest of your deductible: $380\n- 20% coinsurance on the remaining $820: $164\n- The same scan at St. Mary's hospital would be about $884. An MRI needs prior authorization."),
    (r"physical|\bpt\b|therap", "**$40 per visit** for physical therapy.\n- $40 copay per visit, up to 30 visits a year. The deductible doesn't apply.\n- 12 visits comes to about **$480**, and it counts toward your out-of-pocket max ($1,660 left).\n- It needs prior authorization, which I'm handling."),
    (r"fsa|flexible|spend", "You have **$640** left in your FSA, and it doesn't roll over after Dec 31.\n- Your PT copays (about $480) can come straight from it.\n- Other eligible buys: glasses or contacts, sunscreen SPF 15+, first-aid supplies.\n- Want a reminder on Dec 1 to use what's left?"),
    (r"urgent|walk.?in", "**$75 copay** at an in-network urgent care, and the deductible doesn't apply.\n- An ER visit would be **$350** after your deductible.\n- If it's an emergency, go to the ER or call 911."),
    (r"\ber\b|emergency", "An in-network ER visit is **$350 after your deductible** ($380 of it is left).\n- Urgent care is a $75 copay for things that aren't emergencies.\n- If it's an emergency, go to the ER or call 911."),
    (r"refill|pharm|prescri|\bmed", "Your 90-day refill is **$20** at Elm St Pharmacy (preferred network).\n- At Main St Drug, your usual pharmacy, it's about $58.\n- Mail order is also $20 but takes 5 to 7 days.\n- I already moved this refill to Elm St. It's ready Saturday."),
    (r"dental|clean|teeth", "**$0.** Cleanings are covered at 100%, twice a year, and you haven't used either in 2026.\n- Want me to book one before December?"),
    (r"eye|vision|glasses", "Your eye exam is a **$10 copay**, once a year, and you haven't used it in 2026.\n- Glasses or contacts can be paid with your FSA ($640 left)."),
    (r"derm|skin|mole", "Your Oct 7 dermatology visit should cost **$50**, your specialist copay.\n- If Dr. Lee removes or biopsies the mole, that's billed separately at deductible plus 20%."),
    (r"benefit|most out|maximi|save|use my", "Three things to do before Dec 31:\n- Spend your **$640 FSA**. It doesn't roll over.\n- You're **$380** from your deductible.\n- Your dental cleaning and eye exam are both unused. The cleaning is $0.\n\nWant me to book the cleaning?"),
]
CANNED_DEFAULT = "I'd answer that from your plan documents. Try asking about an MRI, physical therapy, urgent care, a refill or your FSA."
