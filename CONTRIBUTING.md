Contributing to Fyrion

Thank you for your interest in contributing to Fyrion!

Fyrion is an open-source Discord bot, and contributions are welcome. Whether you are fixing a bug, improving documentation, adding a feature, or improving tests, please follow the guidelines below.

Code of Conduct

By participating in the Fyrion project, you agree to follow the project's ""CODE_OF_CONDUCT.md"" (CODE_OF_CONDUCT.md).

Please keep discussions respectful, constructive, and focused on improving the project.

---

Getting Started

1. Fork the Repository

Create your own fork of the Fyrion repository on GitHub.

2. Clone Your Fork

git clone https://github.com/yourusername/fyrion.git
cd fyrion

Replace "yourusername" with your GitHub username.

3. Create a Branch

Create a separate branch for your changes:

git checkout -b feature/my-feature

For bug fixes:

git checkout -b fix/my-fix

Avoid making changes directly on the "main" branch.

---

Development Setup

Install Fyrion with its development dependencies:

pip install -e ".[dev]"

If the project provides additional development setup instructions, follow those as well.

---

Making Changes

Before writing code:

1. Understand the existing architecture.
2. Check existing issues and pull requests.
3. Avoid duplicating existing functionality.
4. Keep changes focused and minimal.
5. Consider how the change affects existing servers and configurations.

For larger changes, open an issue or discussion first so the approach can be reviewed before significant development begins.

---

Code Style

Please follow these conventions when contributing:

Python

- Use clear and descriptive names.
- Use type hints for functions and methods.
- Prefer readable code over unnecessarily clever code.
- Keep functions focused on a single responsibility.
- Use absolute imports where appropriate.

Example:

async def get_guild_config(guild_id: int) -> dict:
    ...

Imports

Prefer:

from fyrion.cogs.moderation import Moderation

Instead of:

from ..cogs.moderation import Moderation

Database Queries

Never concatenate untrusted values directly into SQL statements.

Use parameterized queries:

await db.execute(
    "SELECT * FROM guilds WHERE guild_id = ?",
    (guild_id,),
)

Do not use:

await db.execute(
    f"SELECT * FROM guilds WHERE guild_id = {guild_id}"
)

Discord Permissions

Permission checks must be enforced server-side.

Do not rely solely on UI visibility or command presentation to protect privileged functionality.

Respect Discord's role hierarchy and permission model.

---

Secrets and Sensitive Data

Never commit secrets to the repository.

Do not commit:

- Discord bot tokens
- API keys
- Passwords
- ".env" files containing real credentials
- Private database credentials
- Personal access tokens
- Other authentication secrets

Use ".env.example" with placeholder values when documenting configuration.

If you accidentally expose a secret, revoke or rotate it immediately.

---

Testing

Run the test suite before submitting a pull request:

pytest

When adding or modifying functionality:

- Add tests where practical.
- Update existing tests when behavior changes.
- Make sure existing tests continue to pass.
- Test permission-sensitive functionality carefully.
- Avoid introducing regressions.

If a test cannot be added because the functionality requires a live Discord environment, clearly explain how the change was manually tested.

---

Documentation

Update documentation when your change affects:

- Installation
- Configuration
- Commands
- Permissions
- Environment variables
- Database behavior
- Public APIs
- User-facing functionality

Documentation should remain synchronized with the actual implementation.

---

Pull Requests

Before opening a pull request, make sure:

- [ ] The code is focused on the stated change.
- [ ] Tests pass.
- [ ] New functionality has appropriate tests where practical.
- [ ] Documentation has been updated when necessary.
- [ ] No secrets or credentials are included.
- [ ] No unnecessary generated files are included.
- [ ] The change does not introduce obvious security issues.

Pull Request Description

A good pull request should explain:

1. What changed?
2. Why was it changed?
3. How was it tested?
4. Are there any limitations or known issues?

Keep pull requests reasonably small and reviewable whenever possible.

---

Commit Messages

Use clear commit messages that describe the change.

Good:

fix: enforce moderation role hierarchy

feat: add persistent ticket buttons

docs: improve self-hosting instructions

Avoid vague messages such as:

update

changes

fixed stuff

---

Issues and Bug Reports

When reporting a bug, include:

- Fyrion version or commit.
- Python version.
- Operating system/environment.
- Steps to reproduce the problem.
- Expected behavior.
- Actual behavior.
- Relevant error messages or logs.

Never include bot tokens, passwords, API keys, or other secrets in an issue.

For security vulnerabilities, follow ""SECURITY.md"" (SECURITY.md) instead of opening a public issue.

---

Feature Requests

Feature requests are welcome.

A useful feature request should explain:

- What problem the feature solves.
- How the feature would work.
- Why it would benefit Fyrion users.
- Any relevant examples or use cases.

Features should fit Fyrion's goals of being reliable, secure, maintainable, and suitable for self-hosting.

---

Before You Submit

Run:

pytest

Then review your changes:

git diff

Check the files being committed:

git status

Make sure no secrets or unnecessary files are included.

---

Thank You

Every contribution helps improve Fyrion.

Thank you for helping build a better, safer, and more reliable Discord bot.