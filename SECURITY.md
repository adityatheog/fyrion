Security Policy

Security is a first-class requirement for Fyrion.

Supported Versions

Only the latest version on the "main" branch is actively supported with security updates.

Older releases may not receive security fixes.

Version| Supported
"main"| ✅ Yes
Older releases| ❌ No

Reporting a Vulnerability

If you discover a security vulnerability in Fyrion, please do not create a public GitHub issue.

This is especially important for vulnerabilities involving:

- Privilege escalation
- Discord permission or role-hierarchy bypasses
- Authentication or authorization
- Bot-token exposure
- SQL injection
- Sensitive data exposure
- Security controls or AutoMod bypasses

How to Report

Please report vulnerabilities privately using one of the following methods:

1. GitHub Private Vulnerability Reporting, if enabled for the repository.
2. Directly contact the project maintainers through the official project contact method.

When reporting a vulnerability, include:

- A clear description of the issue.
- Steps required to reproduce it.
- The potential security impact.
- Relevant logs, screenshots, or proof-of-concept information when safe to provide.
- The affected Fyrion version or commit.

Please avoid including real bot tokens, passwords, API keys, or other sensitive credentials in your report.

Response Timeline

The maintainers aim to:

- Acknowledge valid security reports within 48 hours.
- Investigate the reported vulnerability.
- Determine its severity and affected versions.
- Develop and test an appropriate fix.
- Release security updates when necessary.

Response times may vary depending on the complexity and severity of the issue.

Responsible Disclosure

Please allow the maintainers reasonable time to investigate and address a vulnerability before publicly disclosing technical details.

Security researchers who responsibly report vulnerabilities will be credited when appropriate, unless they prefer to remain anonymous.

Out of Scope

The following are generally outside the application's security scope:

- Vulnerabilities caused exclusively by an incorrectly configured host environment.
- Issues requiring direct access to the host's ".env" file.
- Issues requiring unauthorized access to the host filesystem.
- Compromise of the operating system or hosting provider.
- Missing rate limits where there is no meaningful security impact.
- Problems caused by insecure third-party hosting configurations.

Infrastructure-level compromises should be reported to the relevant hosting provider or system administrator.

Security Best Practices for Self-Hosters

Self-hosters should:

- Keep the bot token private.
- Never commit ".env" files containing secrets.
- Keep Python and Fyrion dependencies updated.
- Use appropriate Discord permissions.
- Avoid granting unnecessary administrator permissions.
- Protect the host system and filesystem.
- Regularly review bot permissions and server roles.
- Back up important database data securely.

Scope

This policy covers security vulnerabilities in the Fyrion software itself.

Third-party services, Discord's infrastructure, hosting providers, operating systems, and unrelated dependencies may have separate security-reporting procedures.

---

Thank you for helping keep Fyrion and its users secure.