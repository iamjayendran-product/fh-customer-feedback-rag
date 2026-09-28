# Feedback aspect taxonomy — v1

Derived from a rating-stratified read of ~220 UK App Store + Google Play
reviews (2026-09-24). **Frozen once Jay approves it** — changing an aspect
name or definition after tagging has started invalidates prior tags for that
aspect (re-run tagging, bump `skill_version` in `schema.json`).

Every review gets zero or more aspect tags. A short one-liner or an
off-topic review (e.g. "ebike", "ok") may get zero aspects — don't force a
match.

## 1. Delivery Time & Tracking
ETA accuracy, how long delivery actually took, and how well the app
communicates status while waiting (or the lack of a "picked up / on the
way" stage between "cooking" and "delivered").
- *Negative:* "waited 60 minutes and then rang the takeaway to be told
  another 20 minutes", "the tracking is never right, just a guess"
- *Positive:* "as a first time user very intuitive and the timing for
  delivery accurate", "food always on time"

## 2. Order Accuracy
Wrong or missing items, incorrect customisation (e.g. asked for no cheese,
got cheese), items substituted without notice.
- *Negative:* "chicken Tika wrap... clearly stated NO cheese on order
  arrived with cheese", "had items missing from my order"

## 3. Food Quality & Safety
Temperature, freshness, taste, and safety (mould, food poisoning) as
experienced by the diner — tag this even when the underlying cause is the
restaurant's kitchen, not the Foodhub app/platform itself. A roadmap needs
to see the pattern regardless of who's technically at fault; whether the
fix is a partner-quality lever or a product feature is a roadmap decision,
not a tagging one.
- *Negative:* "food was cold, the wrong drinks arrived", "Mouldy food
  delivered... we ended up with food poisoning"
- *Positive:* "food was fresh, tasty and great value"

## 4. Refunds & Complaint Handling
Speed/completeness of refunds, whether the in-app complaint flow works
(e.g. photo-upload requirement blocking submission), and responsiveness of
support (live chat, chatbot, phone).
- *Negative:* "refund in 24 hours" (sarcastic — see Edge Cases), "the app
  would not let me proceed with my complaint... wanted photos", "live chat
  doesn't work. want a refund? good luck."
- *Positive:* "Immediately after that I got an email from Foodhub saying
  that my money had been returned to my account. Excellent service."

## 5. App Reliability & Usability
Crashes, freezes, checkout looping, login/session drops, address/location
detection, navigation/search (including inability to search by dish or
filter by allergen), general "clunky" complaints.
- *Negative:* "New update crashes app", "app continuously logs me out when
  placing orders", "can't even search for a specific dish"
- *Positive:* "easy to use", "very intuitive"

## 6. Payments & Checkout
Payment failing/timing out, being charged more than once, being asked to
re-verify card details repeatedly, not saving a card, discomfort with how
payment data is handled.
- *Negative:* "always take payments more than once watch out", "everytime
  you go to pay it asks for bank verification even though same payment
  details"

## 7. Pricing, Fees & Promotions
Delivery/service/bag fees, discount codes not applying, minimum-spend
confusion, price comparisons to competitors on the *same order*.
- *Negative:* "delivery charges too high", "gave me a discount code for
  checkout then didn't take the code", "advertising 40% discount you
  don't get anything"
- *Positive:* "a lot cheaper than other apps"

## 8. Restaurant Listings & Trust
Fake or duplicate restaurant listings, orders accepted from closed
restaurants, suspected scam patterns (repeat no-shows, no refund path).
- *Negative:* "fake businesses on here... some businesses have taken the
  money and not issue refunds", "allowed me to order from a restaurant
  that has been trying to remove themselves for 2 months"

## 9. Delivery Driver Conduct
Driver behaviour and professionalism specifically (as distinct from #1's
timing/tracking).
- *Negative:* "leave food on the doorstep in the rain", "paid a child over
  the road to drop off my delivery"

## 10. Competitive Comparison
Any review that explicitly compares Foodhub to a named competitor
(Just Eat, Uber Eats, Deliveroo) on price, speed, features or reliability.
Tag this *in addition to* whichever aspect above the comparison is about
— it's a cross-cutting signal, not a replacement.
- *Negative:* "it's not as good as just eat"
- *Positive:* "fast reliable.. much better than Uber and just eat"

---

## Persona (not an aspect — a separate field)
Even though these are all consumer app-store reviews, occasionally a
**restaurant partner** vents there instead of on their own channel (e.g. a
review complaining Foodhub lets competing restaurants run duplicate
listings, written from a business owner's point of view, not a diner's).
Classify `persona` per review by voice and content, never by source alone:
- `diner`: talking about ordering/eating food as a customer.
- `partner`: talking about running a restaurant on Foodhub, payouts,
  contracts, commission, other restaurants' conduct as a competitor.
- `unknown`: can't tell (very short/ambiguous review).

## Edge cases
- **Sarcasm**: "refund in 24 hours" attached to a 1-star review about a
  slow refund is negative, not a literal positive claim — read sentiment
  from the whole review, not keyword-spotting the phrase.
- **Mixed reviews**: tag every aspect that's genuinely present, each with
  its own sentiment. A 5-star review that says "food is usually great,
  except one missing item" gets Food Quality (or Order Accuracy, positive)
  overall positive AND Order Accuracy (negative, low severity).
- **Star/text mismatch**: set `star_text_mismatch=true` when the written
  sentiment clearly contradicts the star rating in direction (not just
  intensity) — e.g. 3 stars with purely positive text ("seamless and
  straightforward. simples."), or 5 stars describing a real complaint with
  no redeeming point. Don't flag ordinary "mostly positive but one gripe"
  reviews — that's normal, not a mismatch.
- **Not about Foodhub**: if a review is clearly about an unrelated app or
  business (rare, since these are pulled from the Foodhub app/package
  listings specifically), set `is_foodhub=false` and skip aspects/severity.
- **Off-topic / too short to classify**: one-word or unrelated reviews
  ("ebike", "ok") can get zero aspects. Don't force a fit.
- **Severity** (1-3, aspect-level, only for negative aspects — leave null
  for positive/neutral): 1 = minor annoyance, 2 = degraded experience but
  resolved/workaroundable, 3 = lost money, lost the order entirely, safety
  issue (food poisoning), or explicit "I'm leaving/uninstalling" language.
