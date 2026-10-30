# Stripe API

`api_stripe.py` exposes three endpoints under `settings.API_STRIPE` (`/stripe`):

- `GET /gate/autocode` — gate for the Kiwi-Code subscription. Returns `true` for a
  `valid` Access row, redirects to `STRIPE_PROCESSING_URL` for a `processing` row, and
  otherwise creates a Stripe Checkout Session and redirects to it.
- `GET /portal/autocode` — redirects to the Stripe Billing Portal for the user's customer.
- `POST /webhook` — signature-verified receiver that keeps the `Access` collection in sync.

## One Stripe customer per user

The gate looks up an existing Stripe customer by the user's email before creating a
Checkout Session, and passes it as `customer`. Without this, Checkout creates a brand-new
customer every time, so a user who cancels and re-subscribes ends up with a second customer
and a split billing history.

This is a convention enforced only at checkout time, not a constraint. Two known ways it
breaks:

### Email change in Supabase

The lookup keys on the email Supabase reports at checkout time. If a user changes their
email after subscribing, the next checkout finds no customer under the new email and
creates a fresh one. That user now has two Stripe customers, and only the newest one is
reachable through the gate. Their old invoices stay under the old customer.

To fix properly, store `stripe_user_id` on the user record and pass it as `customer`
directly, with the email lookup as a fallback only.

### Customers created outside this code

Stripe does not enforce email uniqueness on customers. A customer created from the Stripe
dashboard, by another product sharing the Stripe account, or by a manual API call can share
an email with one of ours. `stripe.Customer.list(email=...)` returns whichever Stripe
orders first, so the gate may attach a new subscription to a customer this code never
created. That still works, but the subscription's metadata (`user_id`, `space_id`) is what
the webhooks trust, not the customer, so attribution stays correct even when the customer
is shared.

The email filter is an exact, case-sensitive match. Supabase emails are lowercase and
Checkout stores what we pass, so casing is only a concern for customers created elsewhere.
