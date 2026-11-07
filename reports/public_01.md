# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.335 | +0.7% | 0.240 | +0.8% | 0.488 | -0.6% | -1% | 56 (47) |
| nw_kyc | 0.380 | +4.6% | 0.238 | -1.7% | 0.624 | +5.7% | 31% | 23 (21) |
| nw_renewal | 0.429 | +2.1% | 0.263 | -0.3% | 0.561 | +2.9% | 18% | 59 (42) |
| ALL | 0.376 | +2.0% | 0.248 | -0.1% | 0.540 | +2.0% | 14% | 138 (110) |

## Changes applied by the controller

6 applied — beneficial: 4, neutral: 1, harmful: 1

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.022 didΔ=+0.000 → **beneficial**
- nw_renewal v2 (controller_approved) dims=['voice'] engageΔ=+0.002 didΔ=+0.000 → **neutral**
- ks_cancel v3 (controller_approved) dims=['welcome_message'] engageΔ=-0.023 didΔ=+0.000 → **harmful**
- nw_renewal v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.066 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['voice'] engageΔ=+0.035 didΔ=+0.000 → **beneficial**
- nw_kyc v3 (controller_approved) dims=['welcome_message'] engageΔ=+0.038 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 6,
  "decisions": {
    "approved": 6,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 6,
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
    "3": 0.329,
    "4": 0.346,
    "5": 0.344,
    "6": 0.351,
    "7": 0.342
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0