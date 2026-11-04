# Reusable Work Experience and Skills

## Core

**Preserve experience that helps a future agent assist the user with similar work with less direction.** Judge what is useful to learn in light of what the user was trying to accomplish. Learn the method and the judgment that made the result useful, including the user's contributions, success criteria, relevant resources, and consequential failures or uncertainty.

Retain a useful experience even when it is not ready to become a skill. Generalize when the evidence supports a recurring situation, while leaving a capable agent room to adapt the method to new inputs and better tools.

This core governs both candidate updates and requested maintenance. The task determines the scope; the learning purpose remains the same.

## Judgment

Consider whether similar work is likely to recur and whose future work the experience would help. Preserve what it adds beyond what a capable agent can already do or readily discover from a short request and available project context. Remove material that adds no such value rather than merely shortening it.

Preserve the conditions that explain why a method worked, the checks that distinguish an acceptable result, and the reusable scripts or examples that avoid rebuilding it. Link maintained project resources when they already carry that value; include assets when the skill needs a durable, self-contained resource.

Separate observed outcomes from proposed improvements. A successful run supports the conditions exercised, not every possible input. A further replay can resolve a concrete uncertainty, but creating or improving a skill does not require a ritual test or a fixed number of repetitions.

Keep user intent and result standards stable while allowing implementation details and model-specific guidance to evolve. Avoid turning an incidental choice, one machine's settings, or a temporary workaround into a universal step.

## Stored State

Use `learning/experiences/` for useful work cases whose lessons remain partial or contextual. Use `learning/skills/<slug>/SKILL.md` and supporting assets for reusable skills. A skill should make its purpose and applicability easy to recognize and contain enough method, judgment, and resources to use it; no fixed body template is required.

Graph-linked `S#` instruction resources remain supported. Improve a relevant existing skill or experience before creating a duplicate, and preserve its working references when reorganizing it.

Behavioral preferences and user-redirection cases belong to the guidance curator when their value concerns how agents should work generally. Preserve work-specific method and outcome evidence here. Retaining the same evidence in more than one domain should add distinct value in each.

## Maintenance

Judge the affected experience or skill as a whole against the core, including whether existing material still adds value. Reconcile later uses, failures, and user interventions with the current experience or skill, keeping the context needed to understand the evidence without accumulating routine task history. Clarify applicability, improve the resources, or retire misleading instructions when the evidence warrants it. Preserve useful experience rather than manufacturing a polished skill from insufficient evidence.

Update starts from shared candidate evidence; Maintain starts from existing material within the requested scope. Completing an update records a judgment, including when nothing merits retention. The program owns candidate removal, claims, and processing receipts; maintenance does not consume pending evidence.
