You are still **{agent_label}**, in the same working session in which you explored
this repository and wrote your plan. Everything you found then still applies — you do
not need to re-read what you already know.

{new_material}

---

# YOUR TURN (round {round_no})

Say something that moves the plan forward. Useful contributions look like:

- naming a concrete flaw in a proposal, and what to do instead;
- pointing out something the others missed about this repository — including anything
  you noticed while exploring that they have not accounted for;
- **changing your mind** when someone has out-argued you — say so plainly;
- resolving a disagreement by checking the repository.

Rules:

- **Verify before asserting.** If a claim about this repository decides the argument —
  yours or someone else's — open the file and check. Quote what you find. Prefer
  checking again over trusting your memory of what you read earlier.
- **Do not restate your plan.** Everyone has read it. Add, object, concede or refine.
- **Do not agree to be agreeable.** Unexamined consensus is the failure mode of this
  format. If you think something is wrong, say so and defend it. Holding your earlier
  position is only worth doing if the arguments still support it.

End your reply with a JSON object, on its own, in a ```json fenced block:

```json
{{
  "comment": "your full contribution as markdown",
  "verdict": "CONTINUE or READY",
  "reason": "if READY: why the discussion has matured; if CONTINUE: what still needs settling"
}}
```

Choose `READY` only when you believe the plan is settled enough to implement — not
merely when you have nothing further to add. If you say `READY`, your `comment` must
end with your final position, since it is what carries into the unified plan.

Choose `CONTINUE` while any point that would change the implementation remains open.
