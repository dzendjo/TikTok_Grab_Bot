"""Handlers module."""
from aiogram import Router

from . import admin
from . import broadcast
from . import channels
from . import payments
from . import user


def setup_routers() -> Router:
    """Setup and combine all routers."""
    main_router = Router(name='main')
    main_router.include_router(admin.router)
    main_router.include_router(broadcast.router)
    main_router.include_router(channels.router)
    # Before user.router: its F.text catch-all would swallow /premium.
    main_router.include_router(payments.router)
    main_router.include_router(user.router)

    return main_router


__all__ = ["setup_routers"]
