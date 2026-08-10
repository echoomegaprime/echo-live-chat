# Security policy

Echo Live Chat is a tenant-safe, embeddable support-chat runtime handling browser visitor
sessions, cross-tenant admin authority, and Stripe billing webhooks. Protecting tenant
isolation, the admin bearer boundary, and Stripe signature verification takes priority over
feature velocity.

## Supported version

Security fixes target the current `main` branch and the currently deployed release. Historical
commits are retained for evidence and are not patched in place.

## Report a vulnerability

Do not open a public issue for a suspected vulnerability. Send a private report to
`security@echo-op.com` with:

- affected revision or endpoint;
- reproduction steps and expected impact;
- whether tenant isolation, the admin bearer, visitor session scoping, or Stripe webhook
  verification may be involved;
- safe contact details for follow-up.
