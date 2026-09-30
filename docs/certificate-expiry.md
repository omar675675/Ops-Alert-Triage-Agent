# Certificate Expiry

## Alert Type
certificate_expiring

## Symptoms
- TLS certificate expires in under 14 days (warning) or under 3 days (critical)
- Alert names like CertExpiringSoon, SSLCertExpiry
- Browser trust errors if the certificate has already expired

## Diagnosis
1. Identify the domain and where TLS terminates: load balancer, reverse proxy, or the application.
2. Check how the certificate was issued: ACME auto-renewal or manual.
3. If auto-renewal is configured, find out why it failed: DNS challenge, port 80 blocked, rate limit.
4. Check the expiry date to judge urgency.

## Resolution
1. If auto-renewal exists and failed for a fixable reason, retry renewal once.
2. Verify the new certificate is being served and the expiry date has moved.
3. If the certificate was issued manually, open a renewal request with the certificate owner.
4. If it has already expired, escalate immediately.

## Risk Level
Low for retrying ACME renewal on non-production. Medium on production, since a reload can drop connections. High for manual certificate replacement.

## Approval
- Auto-approved: retry ACME renewal on non-production domains, verify the served certificate
- Requires human approval: any renewal or reload on a production domain, manual certificate replacement, DNS record changes

## Escalate When
- Renewal fails twice
- The certificate has already expired
- More than one domain is expiring at once, which suggests a systemic issue
- It is a wildcard certificate
