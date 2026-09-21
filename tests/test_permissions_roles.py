"""
Unit tests for the role-management permission helpers.

``can_manage_role`` and ``can_manage_member_roles`` gate every role edit Fyrion
performs. They previously had no tests, so the privilege-escalation guard (a
non-admin handing out an Administrator role) and the ordering of the owner /
self-target / hierarchy bypasses were unverified. These lock that behaviour in.
"""

from fyrion.utils.permissions import can_manage_member_roles, can_manage_role


def _role(make_mock_role, position, *, is_default=False, managed=False, admin=False):
    role = make_mock_role(position)
    role.is_default.return_value = is_default
    role.managed = managed
    role.name = "Test Role"
    role.permissions.administrator = admin
    return role


# ---------------------------------------------------------------------------
# can_manage_role
# ---------------------------------------------------------------------------


def test_manage_role_rejects_everyone(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True)
    role = _role(make_mock_role, 10, is_default=True)

    assert can_manage_role(invoker, role) == "The `@everyone` role cannot be managed."


def test_manage_role_rejects_managed_role(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True)
    role = _role(make_mock_role, 10, managed=True)

    assert "managed by Discord" in can_manage_role(invoker, role)


def test_manage_role_requires_bot_manage_roles(create_mock_member, make_mock_role):
    # Bot lacks Manage Roles even though it outranks the role.
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=False)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True)
    role = _role(make_mock_role, 10)

    assert can_manage_role(invoker, role) == "I need the `Manage Roles` permission for that."


def test_manage_role_bot_must_outrank(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=20, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True)
    role = _role(make_mock_role, 50)  # above the bot

    assert "above my highest role" in can_manage_role(invoker, role)


def test_manage_role_owner_bypasses_everything(create_mock_member, make_mock_role, mock_guild):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    # Owner has no Manage Roles perm and is below the admin role, yet is allowed.
    owner = create_mock_member(mock_guild.owner_id, top_role_position=5, manage_roles=False)
    role = _role(make_mock_role, 10, admin=True)

    assert can_manage_role(owner, role) is None


def test_manage_role_requires_invoker_manage_roles(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=False)
    role = _role(make_mock_role, 10)

    assert can_manage_role(invoker, role) == "You need the `Manage Roles` permission for that."


def test_manage_role_invoker_must_outrank(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=15, manage_roles=True)
    role = _role(make_mock_role, 30)  # above the invoker

    assert "above your highest role" in can_manage_role(invoker, role)


def test_manage_role_non_admin_cannot_grant_admin(create_mock_member, make_mock_role):
    """The privilege-escalation guard: a non-admin with Manage Roles must not
    manage a role that carries Administrator."""
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True, administrator=False)
    role = _role(make_mock_role, 10, admin=True)

    assert "grants `Administrator`" in can_manage_role(invoker, role)


def test_manage_role_admin_can_grant_admin(create_mock_member, make_mock_role):
    create_mock_member(999, top_role_position=100, is_bot=True, manage_roles=True)
    invoker = create_mock_member(123, top_role_position=90, manage_roles=True, administrator=True)
    role = _role(make_mock_role, 10, admin=True)

    assert can_manage_role(invoker, role) is None


# ---------------------------------------------------------------------------
# can_manage_member_roles
# ---------------------------------------------------------------------------


def test_member_roles_bot_unresolved(create_mock_member, mock_guild):
    mock_guild.me = None
    invoker = create_mock_member(123, top_role_position=90)
    target = create_mock_member(456, top_role_position=10)

    assert can_manage_member_roles(invoker, target) == (
        "I could not resolve my own membership in this server."
    )


def test_member_roles_cannot_target_bot(create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=90)

    assert can_manage_member_roles(invoker, bot) == "I cannot change my own roles."


def test_member_roles_bot_must_outrank_target(create_mock_member):
    create_mock_member(999, top_role_position=20, is_bot=True)
    invoker = create_mock_member(123, top_role_position=90)
    target = create_mock_member(456, top_role_position=50)  # above the bot

    assert "so Discord will" in can_manage_member_roles(invoker, target)


def test_member_roles_self_change_allowed(create_mock_member):
    """A member editing their own roles is permitted (the role itself is still
    validated by can_manage_role). This bypass is checked before the
    owner-target and hierarchy guards."""
    create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=50)

    assert can_manage_member_roles(invoker, invoker) is None


def test_member_roles_owner_invoker_bypass(create_mock_member, mock_guild):
    create_mock_member(999, top_role_position=100, is_bot=True)
    owner = create_mock_member(mock_guild.owner_id, top_role_position=10)
    target = create_mock_member(456, top_role_position=80)  # outranks the owner

    assert can_manage_member_roles(owner, target) is None


def test_member_roles_owner_target_protected(create_mock_member, mock_guild):
    create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=90)
    owner = create_mock_member(mock_guild.owner_id, top_role_position=10)

    assert can_manage_member_roles(invoker, owner) == (
        "The server owner's roles cannot be changed through Fyrion."
    )


def test_member_roles_invoker_must_outrank_target(create_mock_member):
    create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=20)
    target = create_mock_member(456, top_role_position=50)

    assert "so you cannot" in can_manage_member_roles(invoker, target)


def test_member_roles_success(create_mock_member):
    create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=80)
    target = create_mock_member(456, top_role_position=10)

    assert can_manage_member_roles(invoker, target) is None
