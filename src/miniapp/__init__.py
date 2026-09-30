"""Modular HTTP backend for the Telegram Mini App."""

from .routes import create_miniapp_blueprint

__all__ = ["create_miniapp_blueprint"]
