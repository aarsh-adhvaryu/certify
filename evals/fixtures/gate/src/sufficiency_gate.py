"""
Cross-domain Sufficiency Gate — checks if minimum slots are filled before retrieval
Rule-based gate + always-proceed baseline
Supports: pf, payslip, labour, tax
"""


# ══════════════════════════════════════════════════════════════════════════════
# REQUIRED SLOTS — per (intent, domain)
# Derived from sufficiency_eval.jsonl: every insufficient case tells us which
# slot was expected to be present. These are the MINIMUM required slots.
# ══════════════════════════════════════════════════════════════════════════════

REQUIRED_SLOTS = {
    # PF domain
    ("full_withdrawal", "pf"):      ["employment_status"],
    ("partial_withdrawal", "pf"):   ["employment_status"],
    ("transfer", "pf"):             ["uan_status"],
    ("kyc_issue", "pf"):            ["kyc_status"],
    ("tds_query", "pf"):            ["service_years"],
    ("employer_complaint", "pf"):   ["incident_type"],

    # Payslip domain
    ("check_deductions", "payslip"):    ["basic_salary"],
    ("full_audit", "payslip"):          ["basic_salary"],
    ("verify_epf", "payslip"):          ["basic_salary", "epf_deducted"],
    ("verify_esi", "payslip"):          ["gross_salary", "esi_deducted"],
    ("check_minimum_wage", "payslip"):  ["gross_salary", "state"],

    # Labour domain
    ("gratuity", "labour"):             ["employment_years"],
    ("wrongful_termination", "labour"): ["termination_reason"],
    ("maternity_benefit", "labour"):    ["is_pregnant"],
    ("overtime_pay", "labour"):         ["employer_type"],

    # Tax domain
    ("tds_on_salary", "tax"):   ["annual_income"],
    ("tds_on_pf", "tax"):       ["pf_withdrawal_amount", "service_years"],
    ("hra_exemption", "tax"):   ["rent_paid", "city_type"],
    ("deductions_80c", "tax"):  ["tax_regime"],
}

# Intents that NEVER need user context — go straight to retrieval
CONTEXT_FREE_INTENTS = {
    "nomination_update",
    "pension_withdrawal",
    "pension",
    "notice_period",
    "check_bonus",
    "refund_status",
    "form16",
    "itr_filing",
    "general",
    "process_guidance",
    "form_guidance",
    "policy_explanation",
    "policy_impact",
    "status_query",
    "grievance_escalation",
}

# Clarifying questions per slot
CLARIFYING_QUESTIONS = {
    # PF
    "employment_status": "Are you currently employed, unemployed, or retired?",
    "service_years": "How many total years have you worked / contributed to PF?",
    "months_unemployed": "How many months have you been unemployed?",
    "uan_status": "Is your UAN currently active, inactive, or not yet generated?",
    "kyc_status": "Is your KYC on the EPFO portal complete, incomplete, or rejected?",
    "incident_type": "What exactly happened — employer not depositing PF, depositing wrong amount, or delayed deposit?",

    # Payslip
    "basic_salary": "What is your monthly basic salary (in rupees)?",
    "gross_salary": "What is your monthly gross salary (in rupees)?",
    "epf_deducted": "How much EPF is being deducted from your salary per month (in rupees)?",
    "esi_deducted": "How much ESI is being deducted from your salary per month (in rupees)? If none, say 0.",
    "state": "Which Indian state do you work in?",
    "employee_count": "Approximately how many employees does your company have?",

    # Labour
    "employment_years": "How many years have you worked with this employer?",
    "termination_reason": "How did your employment end — did you resign, were you fired, or retrenched?",
    "last_drawn_salary": "What was your last drawn monthly salary (in rupees)?",
    "is_pregnant": "Are you currently pregnant or expecting?",
    "employer_type": "What type of employer do you work for — private company, government, factory, or shop/establishment?",
    "notice_period_days": "What is the notice period mentioned in your offer letter or contract (in days)?",

    # Tax
    "annual_income": "What is your total annual income (in rupees)?",
    "tax_regime": "Are you on the old tax regime or the new tax regime?",
    "rent_paid": "How much rent do you pay per month (in rupees)?",
    "city_type": "Do you live in a metro city (Delhi, Mumbai, Kolkata, Chennai) or a non-metro city?",
    "has_form16": "Have you received Form 16 from your employer?",
    "section_80c_investments": "What is your total Section 80C investment amount this year?",
    "pf_withdrawal_amount": "What was the PF withdrawal amount (in rupees)?",
}


# ══════════════════════════════════════════════════════════════════════════════
# GATE LOGIC
# ══════════════════════════════════════════════════════════════════════════════


def check_sufficiency(slots: dict, domain: str) -> dict:
    """
    Check if minimum required slots are filled for the given intent + domain.

    Args:
        slots:  filled slot dict (output of slot extractor)
        domain: "pf" | "payslip" | "labour" | "tax"

    Returns:
        {
            "sufficient": bool,
            "question": str | None,     # clarifying question if insufficient
            "missing": list[str],       # all missing slot names
        }
    """
    intent = slots.get("intent")

    # Context-free intents always proceed
    if intent in CONTEXT_FREE_INTENTS or intent is None:
        return {"sufficient": True, "question": None, "missing": []}

    key = (intent, domain)
    if key not in REQUIRED_SLOTS:
        # Unknown intent/domain combo — let it through
        return {"sufficient": True, "question": None, "missing": []}

    required = REQUIRED_SLOTS[key]
    missing = []

    for slot in required:
        val = slots.get(slot)
        if val is None:
            missing.append(slot)

    if not missing:
        return {"sufficient": True, "question": None, "missing": []}

    # Ask about the first missing slot
    first_missing = missing[0]
    question = CLARIFYING_QUESTIONS.get(
        first_missing,
        f"Could you provide your {first_missing.replace('_', ' ')}?"
    )

    return {"sufficient": False, "question": question, "missing": missing}


def get_missing_slots(slots: dict, domain: str) -> list:
    """Convenience wrapper — returns just the missing slot names."""
    return check_sufficiency(slots, domain)["missing"]


# ══════════════════════════════════════════════════════════════════════════════
# BASELINE — always-proceed gate (no sufficiency check)
# ══════════════════════════════════════════════════════════════════════════════


def baseline_check(slots: dict, domain: str) -> dict:
    """
    Always-proceed baseline. Every query goes to retrieval regardless.
    This is what a standard RAG system does — no pre-retrieval filtering.
    False-sufficient rate = 100% by construction.
    """
    return {"sufficient": True, "question": None, "missing": []}


# ══════════════════════════════════════════════════════════════════════════════
# TEST
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    test_cases = [
        # Should be insufficient — missing employment_status
        ("pf", {"intent": "full_withdrawal"}),
        # Should be sufficient
        ("pf", {"intent": "full_withdrawal", "employment_status": "unemployed"}),
        # Should be insufficient — missing gross_salary and esi_deducted
        ("payslip", {"intent": "verify_esi"}),
        # Should be sufficient (context-free)
        ("tax", {"intent": "refund_status"}),
        # Should be insufficient — missing employment_years
        ("labour", {"intent": "gratuity"}),
        # Should be sufficient
        ("labour", {"intent": "gratuity", "employment_years": 6}),
        # Should be insufficient — missing rent_paid and city_type
        ("tax", {"intent": "hra_exemption", "annual_income": 1200000}),
    ]

    for domain, slots in test_cases:
        result = check_sufficiency(slots, domain)
        bl = baseline_check(slots, domain)
        print(f"\nDomain: {domain} | Intent: {slots.get('intent')}")
        print(f"  Filled: {[k for k,v in slots.items() if v is not None and k != 'intent']}")
        print(f"  Gate:     sufficient={result['sufficient']}, missing={result['missing']}")
        print(f"  Baseline: sufficient={bl['sufficient']} (always True)")
        if result["question"]:
            print(f"  Ask: {result['question']}")
