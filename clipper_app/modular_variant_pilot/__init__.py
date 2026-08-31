from .repository import ModularVariantPilotRepository

__all__ = ["ModularVariantPilotConflict", "ModularVariantPilotRepository", "ModularVariantPilotService"]


def __getattr__(name: str):
    if name in {"ModularVariantPilotConflict", "ModularVariantPilotService"}:
        from .service import ModularVariantPilotConflict, ModularVariantPilotService
        return {"ModularVariantPilotConflict": ModularVariantPilotConflict, "ModularVariantPilotService": ModularVariantPilotService}[name]
    raise AttributeError(name)
