"""
Clinic vertical config — persona, knowledge base, info schema, brand,
demo credentials. Per DEMOSITEMAP.md these are the per-vertical knobs
that scope an otherwise-shared Live Rep service to clinic-flavoured
conversations.

This file holds the *defaults* / template. Mutations from the in-app
Settings page go into `data/demos/restaurant.json` (the runtime DB) which
overrides what's here at request time.
"""
from __future__ import annotations

SLUG = "restaurant"
DISPLAY_NAME = "Restaurant"

# Hard-coded demo credentials. Anyone with the URL can log in — this
# is a demo product, not real auth. See DEMOSITEMAP.md non-goals.
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "demo"

# Live Rep persona — what Lena sounds like for clinic calls.
PERSONA = (
    "You are the front-desk receptionist for a busy multi-specialty "
    "clinic. Speak warmly, professionally, and efficiently. Patients "
    "call to book or reschedule appointments, ask about specialists, "
    "or follow up on results. Do not invent medical advice — for "
    "clinical questions, say a doctor will follow up. Keep replies "
    "short and conversational."
)

# Reference material Lena reads during a call.
KNOWLEDGE = """\
## About the clinic
Multi-specialty clinic. Pediatrics, cardiology, dermatology, general
practice, dental.

## Standard appointment lengths
- Consultation: 30 min
- Follow-up: 15 min
- Procedure: 45-60 min

## Hours
- Mon-Fri: 8am-8pm
- Sat: 9am-2pm
- Sun: closed
"""

# Structured info the agent tries to collect each call.
INFO_SCHEMA = [
    {"name": "patient_name",     "label": "Patient name",
     "description": "Full name of the patient."},
    {"name": "phone",            "label": "Phone",
     "description": "Best phone number to reach the patient."},
    {"name": "reason",           "label": "Reason for call",
     "description": "Why they are calling — symptoms, follow-up, scheduling, etc."},
    {"name": "preferred_doctor", "label": "Preferred doctor / specialty",
     "description": "Specific doctor or specialty requested, if any."},
    {"name": "preferred_time",   "label": "Preferred appointment time",
     "description": "When they would like to come in."},
]

GREETING = "Hello, thank you for calling. How can I help you today?"

# UUID prefix the FreePBX dialplan uses for this vertical. The Live Rep
# service routes by prefix when we extend it in Phase 2 of the plan.
UUID_PREFIX = "clinic-demo-"
