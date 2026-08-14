# Scoring codegen — Zone B only (I1)

Write a single Python module that ranks replacement flight **offers by id**.

## Contract

Define exactly:

```python
def score(payload: dict) -> list[dict]:
    ...
```

`payload` matches the executor `ScoringInput` JSON shape:

- `case_ref` (str)
- `candidates` (list of dicts with `offer_id`, `price`, `currency`, `arrival_at`, `stop_count`, `min_transfer_minutes`, `origin`, `destination`, `carriers`)
- `must_arrive_by` (str | null)
- `budget_ceiling_sgd` (number)
- `mobility_penalty_weight` (float)
- `original_arrival_at` (str)

Each returned dict must include:

- `offer_id` (str) — copy from input candidates only; never invent ids
- `score` (float) — higher is better
- `components` (dict[str, float]) — explainable partials
- `self_transfer_risk` (float 0–1)
- `mobility_fit` (float 0–1)

## Hard rules

- **Stdlib only.** No third-party imports. Prefer **no imports at all**.
- No network, no filesystem writes, no subprocess, no dynamic code execution (`eval` / `exec` / `__import__`).
- Do not invent offer ids, flight numbers, carriers, or prices. Use only fields on each candidate.
- Prefer earlier arrival (vs `original_arrival_at` / `must_arrive_by`), lower price vs `budget_ceiling_sgd`, fewer stops, and higher `mobility_fit` when `mobility_penalty_weight` is high.

## Output

Return **only** the Python source for the module. No markdown fences, no commentary before or after the code.

Keep the module under ~80 lines. Prefer a compact loop with clear component floats — do not add helper parsers unless necessary.
