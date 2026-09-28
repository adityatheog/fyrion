    guild = access.guild
    if guild is None:
        object_fields = {
            field: value
            for field, value in values.items()
            if value is not None and (field in CHANNEL_FIELDS or field in ROLE_FIELDS)
        }
        if object_fields:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This server is not currently available for live validation. "
                "Channel and role settings cannot be updated until the server is "
                "reachable again.",
            )

    if guild is not None:
