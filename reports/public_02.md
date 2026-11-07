# Grade report — public_02_incidents

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.356 | +44.0% | 0.239 | +30.2% | 0.510 | +7.0% | 40% | 115 (330) |
| nw_kyc | 0.378 | +1.0% | 0.247 | +2.0% | 0.586 | -1.1% | -25% | 35 (31) |
| nw_renewal | 0.420 | -1.4% | 0.262 | -0.1% | 0.542 | -1.1% | -14% | 55 (55) |
| ALL | 0.382 | +14.4% | 0.248 | +11.9% | 0.536 | +1.2% | 16% | 205 (416) |

## Changes applied by the controller

11 applied — neutral: 1, harmful: 4, beneficial: 6

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=-0.018 didΔ=+0.000 → **harmful**
- ks_cancel v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.016 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.059 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=-0.039 didΔ=+0.000 → **harmful**
- nw_renewal v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.111 didΔ=+0.000 → **beneficial**
- nw_renewal v5 (controller_approved) dims=['welcome_message'] engageΔ=-0.100 didΔ=+0.000 → **harmful**
- ks_cancel v5 (controller_approved) dims=['caller_id'] engageΔ=+0.000 didΔ=+0.599 → **beneficial**
- ks_cancel v6 (controller_approved) dims=['prompt_version'] engageΔ=+0.102 didΔ=+0.000 → **beneficial**
- nw_renewal v7 (controller_approved) dims=['voice'] engageΔ=-0.044 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 11,
  "decisions": {
    "approved": 11,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 11,
  "auto_applied": 0,
  "auto_apply_rejected": 67,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 3,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[
  {
    "type": "did_flagged",
    "workflow": "ks_cancel",
    "at": "D2 10:00",
    "flagged_did_share_by_day": {
      "2": 0.502,
      "3": 0.234,
      "4": 0.0,
      "5": 0.0,
      "6": 0.0,
      "7": 0.0,
      "8": 0.0,
      "9": 0.0
    }
  },
  {
    "type": "supervisor_edit",
    "workflow": "ks_cancel",
    "at": "D3 11:30",
    "mttr_minutes": 1440,
    "fixed_by": "controller_approved"
  },
  {
    "type": "supervisor_edit",
    "workflow": "nw_renewal",
    "at": "D5 15:00"
  }
]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "2": 0.325,
    "3": 0.313,
    "4": 0.32,
    "5": 0.327,
    "6": 0.326,
    "7": 0.338,
    "8": 0.338,
    "9": 0.325
  }
}
```

Credits used: 5 · validation runs: 1 · LLM fix calls: 1