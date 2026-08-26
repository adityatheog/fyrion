"""
Reusable confirmation prompt.

Destructive administrative actions (deleting a channel or a role, editing every
member in a server) go through this view first. Two properties matter:

* only the member who invoked the command can press the buttons, checked in
  :meth:`ConfirmView.interaction_check` rather than relying on the ephemeral
  message being private;
* the view always resolves. Either a button is pressed or the timeout fires, so
  the calling command never awaits forever.

The view is intentionally *not* persistent: an abandoned confirmation must
expire rather than survive a restart and be actioned later.
"""
from __future__ import annotations

import discord

DEFAULT_TIMEOUT = 60.0


class ConfirmView(discord.ui.View):
    """A yes/no prompt bound to a single member."""

    def __init__(
        self,
        author_id: int,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        confirm_label: str = "Confirm",
        cancel_label: str = "Cancel",
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.danger,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool = False

        self.confirm_button.label = confirm_label
        self.confirm_button.style = confirm_style
        self.cancel_button.label = cancel_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "\u274c Only the person who ran the command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    def _disable(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = True
        self._disable()
        # Editing here removes the buttons immediately, so a second click cannot
        # queue the same destructive action twice.
        await interaction.response.edit_message(view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.value = False
        self._disable()
        await interaction.response.edit_message(view=None)
        self.stop()


__all__ = ["ConfirmView", "DEFAULT_TIMEOUT"]
