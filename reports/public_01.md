# Grade report — public_01_dev

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.346 | +4.0% | 0.240 | +0.6% | 0.503 | +2.4% | 14% | 53 (47) |
| nw_kyc | 0.373 | +2.7% | 0.240 | -0.7% | 0.611 | +3.4% | 8% | 16 (21) |
| nw_renewal | 0.415 | -1.1% | 0.261 | -0.8% | 0.544 | -0.4% | 0% | 44 (42) |
| ALL | 0.375 | +1.8% | 0.247 | -0.2% | 0.538 | +1.5% | 7% | 113 (110) |

## Changes applied by the controller

3 applied — beneficial: 3

- ks_cancel v2 (controller_approved) dims=['voice'] engageΔ=+0.017 didΔ=+0.000 → **beneficial**
- nw_kyc v2 (controller_approved) dims=['prompt_version'] engageΔ=+0.030 didΔ=+0.000 → **beneficial**
- ks_cancel v3 (controller_approved) dims=['voice'] engageΔ=+0.060 didΔ=+0.000 → **beneficial**

## Governance

```
{
  "approval_requests": 3,
  "decisions": {
    "approved": 3,
    "expired": 0
  },
  "rejection_reasons": {},
  "applied_via_approval": 3,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "ks_cancel": 1,
    "nw_kyc": 1
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{}
```

Credits used: 8 · validation runs: 1 · LLM fix calls: 1