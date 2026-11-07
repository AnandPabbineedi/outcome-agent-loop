# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.348 | +4.5% | 0.243 | +2.2% | 0.496 | +1.1% | 0% | 56 (47) |
| nw_kyc | 0.383 | +5.3% | 0.239 | -1.4% | 0.630 | +6.7% | 28% | 14 (21) |
| nw_renewal | 0.423 | +0.7% | 0.262 | -0.5% | 0.559 | +2.5% | 18% | 41 (42) |
| ALL | 0.380 | +3.2% | 0.249 | +0.5% | 0.544 | +2.7% | 13% | 111 (110) |

## Changes applied by the controller

8 applied — beneficial: 5, harmful: 2, neutral: 1

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['welcome_message'] engageΔ=-0.023 didΔ=+0.000 → **harmful**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.066 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- ks_cancel v4 (controller_approved) dims=['voice'] engageΔ=+0.062 didΔ=+0.000 → **beneficial**
- ks_cancel v5 (controller_approved) dims=['voice'] engageΔ=-0.058 didΔ=+0.000 → **harmful**

## Governance

```
{
  "approval_requests": 9,
  "decisions": {
    "approved": 8,
    "rejected": 1,
    "expired": 0
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 1
  },
  "applied_via_approval": 8,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "nw_renewal": 2,
    "nw_kyc": 2,
    "ks_cancel": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nw_renewal:welcome_message=W3": {
    "3": 0.351,
    "4": 0.338,
    "5": 0.327,
    "6": 0.341,
    "7": 0.344
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0