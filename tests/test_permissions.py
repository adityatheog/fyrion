"""
Unit tests for the Discord permission and hierarchy validation logic.
"""
from fyrion.utils.permissions import can_moderate

def test_cannot_moderate_self(create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=50)
    
    # Target is the invoker
    error = can_moderate(invoker, invoker)
    assert error == "You cannot moderate yourself."

def test_cannot_moderate_bot(create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=150)
    
    error = can_moderate(invoker, bot)
    assert error == "I cannot moderate myself."

def test_cannot_moderate_owner(mock_guild, create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=150)
    owner = create_mock_member(mock_guild.owner_id, top_role_position=200)
    
    error = can_moderate(invoker, owner)
    assert error == "The server owner cannot be moderated."

def test_hierarchy_invoker_too_low(create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=10) # Low role
    target = create_mock_member(456, top_role_position=20)  # Higher role
    
    error = can_moderate(invoker, target)
    assert error == "You cannot moderate a member with an equal or higher role than yourself."

def test_hierarchy_bot_too_low(create_mock_member):
    bot = create_mock_member(999, top_role_position=30, is_bot=True) # Bot has lower role than target
    invoker = create_mock_member(123, top_role_position=100) # Invoker has admin role
    target = create_mock_member(456, top_role_position=50)   # Target role is above bot
    
    error = can_moderate(invoker, target)
    assert error == "I cannot moderate a member with an equal or higher role than my highest role."

def test_successful_hierarchy(create_mock_member):
    bot = create_mock_member(999, top_role_position=100, is_bot=True)
    invoker = create_mock_member(123, top_role_position=80)
    target = create_mock_member(456, top_role_position=10)
    
    # Valid moderation action
    error = can_moderate(invoker, target)
    assert error is None
