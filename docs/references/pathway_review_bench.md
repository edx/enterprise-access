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

`replacements` is the field that separates the two failure modes. A course key means the
ranker had better content in that rung and missed it; an empty string means the reviewer found
nothing usable, so the catalog is the problem. Those need different fixes and must never be
summed together.

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
