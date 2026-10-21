# Claims vs. reality: when do AI agents misreport their own work?

**Setup.** Three agent models worked 924 episodes of small coding, data-analysis and operations tasks. Each task came in a possible, an impossible and a partly impossible variant, under four conditions: neutral, user pressure, a nearly spent tool budget, and a flaky tool. A system-call ledger the agent cannot touch recorded what each agent actually did. Exact graders and that ledger, never an LLM, decided whether its final report was true. The design, the definitions and every change to them were pre-registered with dated amendments.

**Headline findings**

1. **Misreports are rare and mechanism-specific.** After hand-checking, 0–3% of episodes per model and report format contain a misreport (95% intervals up to 7%). The 13 confirmed cases come from three mechanisms:
   - an agent's own database call creates an empty file where a missing input should be, and the agent reports the input as provided but empty (9 cases, read as a mistaken belief);
   - a validator result stated without the validator ever running (3);
   - one service's health reported from another service's status (1).
2. **Rubric judges rarely flag honest work, but caught at most 1 of the 3 mechanisms.** LLM transcript judges using rubrics built from published definitions flagged 0–1.9% of honest episodes. The main judge caught 0 of 8 judged misreports (95% CI 0–0.32), and a stronger judge caught 1 of 8 (0.02–0.47). The positives are few and correlated, so these are mechanism-level results, not a general recall estimate.
3. **An exact action ledger catches all three mechanisms**, because each is a claim about an action or observation that the record shows did not happen as stated.
4. **Judges and a claim-by-claim reading pass find real misreports that the exact definitions miss.** Examples: "verified by recomputing separately" when only one computation ran, a search result reported from a command that never started, and a pre-restart health check presented as post-restart. The exact detectors and the LLM reviewers see different failures, and neither replaces the other.

**What did not show up.** No pre-registered effect of user pressure, budget, flaky tools or report format reached significance, and no model ranking is supported: all intervals overlap.

**Checks.** Human labels agree closely with the LLM claim extractor (κ 0.81–1.00 on overall and per-item status; n = 30 honest and n = 18 mixed messages). Human claims reproduced all 4 claim-dependent misreports; that check was not fully blind. The tasks are small and synthetic, so these are measurements in a controlled setting, not deployment rates.
