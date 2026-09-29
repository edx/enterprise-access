# Pathway review bench

An internal surface where staff reviewers judge assembled career pathways, so that approved
ones can be used as worked examples for the recommendation pipeline. This document covers the
data layer; the reviewer UI is a separate change.

## What it is, and what it deliberately is not

The bench collects human judgements. Following the eval-harness pattern
(`architecture-patterns.md` #16), **it owns no pipeline logic** — ladders are assembled by the
pathway pipeline and loaded here as fixtures. Nothing in `apps/pathway_review` retrieves,
ranks or assembles. If it did, the bench would be measuring itself.

It also carries no dependency on the `pathways` or `pathway_eval` apps, which is why this work
can live on a branch off `main` while the pipeline is still in review.

## Blinding is a database boundary, not a build step

Roughly one in ten queue items is a **seeded control**: a real ladder with two rungs swapped
for courses from an unrelated career. Reviewers who approve them were not reading, which is
the only way to tell a careful reviewer from a fast one when the output is consensus data.

That only works if reviewers cannot tell which items are planted. Two columns on
`PathwayReviewItem` therefore never reach a browser:

| Column | Holds | Served? |
| --- | --- | --- |
| `payload` | courses, per-rung alternates, descriptions | yes — this column only |
| `pool` | `reach` / `tail` / `control` | never |
| `control_key` | which rungs were corrupted | never |

`tier` exists for queue ordering and deliberately does **not** separate controls from real
items: controls share the reach tier so they interleave.

Each item's `payload` is self-contained, descriptions included, so the bench can serve one
pathway per request rather than shipping the whole queue to the browser.

## Sampling, and why `weight` exists

Items come from two pools that must not be averaged together:

* **reach** — a census of the highest-traffic pathway families. `weight` is 1.0.
* **tail** — a stratified probability sample of everything else, allocated disproportionately
  across the level mixes so the rarer flat shapes have enough n to say anything. `weight`
  undoes that allocation when estimating a catalog-wide rate.

A mean taken over both pools without `weight` is wrong, and wrong in the flattering direction:
the reach pool holds the families with the deepest catalog coverage.

## A verdict that is not positive owes an explanation

`PathwayReviewVote.clean()` rejects a `needs_work` or `bad` verdict with empty notes. A
downvote with no diagnosis cannot be acted on — it tells you the accuracy and nothing about
what to change. `skip` is exempt: "I can't judge this" is a legitimate answer, and forcing
prose there would push reviewers into guessing rather than skipping.

`replacements` is the field that separates the two failure modes. A pick means the ranker had
better content in that rung and missed it; `"__none__"` means the reviewer found nothing
usable, so the catalog is the problem. Those need different fixes and must never be summed
together. What a pick holds is described in
[Best replacement, plus up to three that would also do](#best-replacement-plus-up-to-three-that-would-also-do).

## Best replacement, plus up to three that would also do

A reviewer who drops a course is shown the other courses the search found at that rung's level
and asked what should have been there. They can mark:

* one **best** replacement — the first course they click;
* up to three more as **also fine** — each later click toggles one, and a fourth is refused;
* or **nothing here would work**, which is exclusive: choosing it clears any picks, and
  choosing a course clears it.

Clicking the best again un-sets it and promotes the first also-fine pick. An also-fine pick
carries a "Make best" control that swaps it with the current best.

The first version allowed one pick per rung, which cannot record that more than one alternate
would have done. A flat multi-select was considered and rejected: it records which courses
would do, but not which one the reviewer would have put in the rung, and that is the answer a
ranker's choice can be scored against. Best plus also-fine keeps both.

### Stored shape

`PathwayReviewVote.replacements` maps a dropped step number (as a string) to:

| Value | Means |
| --- | --- |
| `{"best": key, "also": [keys]}` | the ranker missed better content in this rung |
| `"__none__"` | nothing the search found would do; the catalog lacks it |
| *(step absent)* | the reviewer dropped the course but left the rung unanswered |

`submit_vote` rejects, with a 400 that names the problem:

* a step the pathway does not have, or one the reviewer kept;
* a key that is not among the alternates the bench showed for that step's level
  (`payload["alt"][level]`) — stored, it would read as "the ranker missed this" for a course
  the reviewer never saw;
* also-fine picks with no best, more than three of them, a repeated one, or the best repeated
  among them.

### Older votes

No migration and no data edit: the field is a `JSONField`, and both shapes are read through
one helper. The first client sent three values, and each keeps the meaning it had:

| First client sent | Meant | Read as |
| --- | --- | --- |
| a bare course key | one replacement | `{"best": key, "also": []}` |
| `"__none__"` | nothing here would work | `"__none__"`, unchanged |
| `""` | dropped, but left unanswered | no pick: the step is left out |

`"__none__"` stays the nothing-works value for that reason. Moving it to `""` would have given
`""` two meanings in stored votes, and only a data edit could have separated them.

* The server still accepts the first client's shape, since a tab left open across a deploy
  keeps sending it: a bare course key is stored as a best pick, and an incoming `""` is dropped
  rather than stored.
* Stored votes are read through `PathwayReviewVote.replacement_picks`
  (`models.normalize_replacements`), which returns the current shape for either and leaves out
  the unanswered steps.

## Suggesting courses without dropping one

A replacement answers "this course is wrong, and this is what should be here". Reviewers also
had the opposite case: the course is fine, but others on the same rung would be just as good.
Forcing that through Drop would record a correction that isn't one, and would inflate every
count of drops. So a kept course has an optional **Suggest** button.

- **The menu:** the same menu of that rung's alternates. The reviewer marks up to three as also
  fine.
- **What it leaves out:** there is no best (the kept course is the first choice) and no "nothing
  works" (nothing is missing).
- **Storage:** a separate field, `PathwayReviewVote.suggestions`, `{step: [keys]}`. It holds
  kept steps only; a dropped step's alternatives stay in `replacements`. Migration
  `0002_pathwayreviewvote_suggestions` adds it, empty for earlier votes.
- **Validation:** as for replacements. Each key must be an alternate the rung offered, at most
  three per step, with no repeats. A suggestion on a dropped step is refused.
- **Changing your mind loses nothing:**
  - Dropping a course whose suggestions are already marked turns them into its replacements,
    the first becoming the best.
  - Keeping a dropped course turns its replacements back into suggestions (the first three, with
    a note if one had to go).

For analysis, a suggestion widens the set of acceptable answers for a rung. It is a softer
signal than a replacement, and nothing that counts drops or swaps should read it as one.

## Access

Two independent gates, both required (`pathway_review/permissions.py`):

1. the `enterprise_access.pathway_review_bench` waffle flag, so the surface can be switched
   off without a deploy;
2. the `pathway_review.add_pathwayreviewvote` model permission, granted to a reviewer group
   in Django admin.

The model permission is used rather than an edx-rbac feature role because the bench has no
enterprise-customer scope — the roles in `core.constants` answer "which customer is this user
an admin of", which is not the question. It is not gated on `is_staff` either: curriculum
reviewers should rate pathways without being handed the Django admin.

## The interface is deliberately untranslated

The repo's convention is to wrap user-facing strings for translation — both admin templates
under `templates/subsidy_access_policy/` use `{% trans %}`, and `make validate_translations`
runs in CI. The bench does not follow it, on purpose.

Every reviewer is a member of 2U's curriculum staff, and the queue itself is filtered to
`language:"English"` before a pathway ever reaches one — the thing being judged is English
course metadata, in English, by English-speaking colleagues. Wrapping roughly a hundred
strings across the template, the views and 700 lines of JavaScript would add real noise for
nobody. Extraction confirms the position is consistent rather than accidental: `makemessages`
across both the `django` and `djangojs` domains pulls nothing out of this app.

If the bench is ever pointed at a non-English catalogue or opened to reviewers outside that
group, this is the decision to revisit first.

## Loading the queue

```bash
./manage.py load_pathway_review_queue --path /path/to/r1_review_queue.json
```

The file is produced by the offline measurement scripts. Program matches in the same file are
ignored — those are reviewed separately. Re-running upserts by `item_id`; pass
`--deactivate-missing` to retire items that have dropped out of a newer queue.

## The surface

Mounted at `/pathway-review/`, session-authenticated, staff-facing. Plain Django views rather
than DRF viewsets: this is an internal HTML tool for named reviewers, not a customer-facing
API, so it needs neither the enterprise-scoped role machinery nor a published schema.

| Route | Does |
| --- | --- |
| `GET /pathway-review/` | the bench page |
| `GET api/next/` | the next pathway for this reviewer, plus their progress |
| `POST api/vote/` | record one judgement |
| `POST api/goal/` | set this reviewer's own goal |
| `GET api/leaderboard/` | who has reviewed the most, and which families they covered |

Every route answers **404**, not 403, when the gate fails — except the page itself for a
signed-out visitor, which redirects to SSO so a reviewer following a link does not hit a dead
end. The bench is meant to be invisible
to people who may not use it, and a 403 still tells you it is there.

### Queue order lives on the server

`selectors.next_item_for` orders by least-reviewed, then tier, then reach. The earlier
prototype balanced its own queue in the browser, which meant shipping every reviewer's votes
to every other reviewer — the opposite of the independence a two-rater design depends on. The
browser is now told only which item to rate next.

### What the leaderboard may say

Families reviewed, never verdicts. Seeing *that* a colleague rated Project Manager is
harmless; seeing *how* they rated it would let the next reviewer anchor on it and would
contaminate the inter-rater agreement the study exists to measure. `test_views` asserts the
response body contains no verdict text.

### Reading the controls

```bash
./manage.py report_pathway_review_controls
```

Scores each reviewer against the seeded controls — how many they saw, and how many they
caught. "Caught" means they did not wave the item through: any verdict other than `good`, or
a drop landing on a rung that was actually corrupted.

It is deliberately a command rather than a panel in the bench: it exists so "who reviewed the
most" can be read next to "who was actually reading". A leaderboard rewards volume, and volume
is exactly what the controls keep honest. Someone who passed the controls needs their other
ratings treated with suspicion rather than merely discounted — consensus gold data cannot
detect them, because their ratings look like agreement.
