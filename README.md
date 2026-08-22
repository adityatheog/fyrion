Fyrion

«Powerful tools for better Discord communities.»

Fyrion is a modern, secure, multi-guild Discord bot built with Python and "discord.py". It is designed from the ground up for safe self-hosting, with strict permission boundaries and data isolation between servers.

Features

🟢 Implemented & Tested

- Secure Moderation — Kick, ban, timeout, warn, and purge commands with Discord role-hierarchy enforcement.
- Ticket System — Persistent, button-based support ticket creation with automatic private-channel generation.
- Invite Tracking — Delta-based in-memory caching for tracking joins, leaves, and net invite changes.
- Security & AutoMod — Regex-based anti-link protection with user, role, and channel whitelisting.
- Server Configuration — Configurable autorole assignment and customizable welcome messages.
- Dynamic Help — Dropdown-based help menu that dynamically reflects loaded modules.

🟡 Planned

- Advanced AutoMod features, including spam and raid protection.
- PostgreSQL database support.
- Web dashboard.

---

Installation & Self-Hosting

Requirements

- Python 3.10 or newer
- A Discord bot application
- A Discord bot token

1. Discord Developer Portal Setup

Before running Fyrion, configure your bot application in the "Discord Developer Portal" (https://discord.com/developers/applications).

1. Open your Fyrion bot application.
2. Go to the Bot section.
3. Enable Server Members Intent.
   - Required for features such as autorole, welcome messages, and invite tracking.
4. Enable Message Content Intent.
   - Required for the anti-link AutoMod system.
5. Generate your bot token.
6. Keep your bot token private.

«⚠️ Never share your bot token or commit it to Git.»

2. Clone the Repository

git clone https://github.com/yourusername/fyrion.git
cd fyrion

3. Install Dependencies

pip install -e .

For development dependencies:

pip install -e ".[dev]"

4. Configure the Environment

Copy the example environment file:

cp .env.example .env

Edit ".env" and add your bot token:

DISCORD_TOKEN=your_token_here

5. Run Fyrion

Start the bot with:

python -m fyrion

On startup, Fyrion will initialize its local SQLite database ("fyrion.db") and synchronize its application commands.

---

Discord Permissions

When inviting Fyrion to a server, make sure the bot has the permissions required by the features you intend to use.

For moderation features, the bot's highest role must also be positioned appropriately in the server's role hierarchy.

«Important: Discord role hierarchy always applies. Fyrion cannot moderate users whose highest role is equal to or higher than the bot's highest role.»

---

Configuration

Fyrion uses environment variables for sensitive configuration.

Example:

DISCORD_TOKEN=your_token_here

Never commit ".env" files containing real credentials.

The repository should contain an ".env.example" file containing safe placeholder values instead.

---

Database

Fyrion currently uses SQLite for local persistent storage.

The database is created automatically when Fyrion starts.

Default database:

fyrion.db

PostgreSQL support is planned for future releases.

---

Project Structure

A typical Fyrion installation follows this structure:

fyrion/
├── fyrion/
│   ├── __init__.py
│   ├── __main__.py
│   └── ...
├── tests/
├── .env.example
├── .gitignore
├── LICENSE
├── README.md
├── SECURITY.md
├── CONTRIBUTING.md
└── pyproject.toml

The exact structure may change as the project evolves.

---

Development

Install Fyrion with development dependencies:

pip install -e ".[dev]"

Run the test suite:

pytest

Before submitting a pull request, ensure that:

- Tests pass.
- No secrets or tokens are committed.
- New functionality has appropriate tests where practical.
- Code follows the project's style and typing conventions.

See ""CONTRIBUTING.md"" (CONTRIBUTING.md) for contribution guidelines.

---

Security

Security is a core requirement of Fyrion.

If you discover a security vulnerability, do not publicly disclose it through a GitHub issue.

Please report security vulnerabilities privately through the project's designated security-reporting method.

See ""SECURITY.md"" (SECURITY.md) for the complete security policy.

---

Contributing

Contributions are welcome.

You can contribute by:

- Reporting bugs.
- Suggesting features.
- Improving documentation.
- Fixing issues.
- Adding tests.
- Submitting pull requests.

Please read ""CONTRIBUTING.md"" (CONTRIBUTING.md) before contributing.

---

License

Fyrion is open-source software licensed under the MIT License.

See ""LICENSE"" (LICENSE) for the complete license text.

---

Built With

- "discord.py" (https://github.com/Rapptz/discord.py) — Discord API wrapper for Python.
- "aiosqlite" (https://github.com/omnilib/aiosqlite) — Asynchronous SQLite database interface.

---

Disclaimer

Fyrion is provided as open-source software for legitimate Discord server administration and community management.

The project maintainers are not responsible for misuse, incorrect configuration, or damages resulting from self-hosting or operating the bot.

---

Fyrion — Powerful tools for better Discord communities.