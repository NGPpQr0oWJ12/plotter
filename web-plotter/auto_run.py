"""Read whether Auto Run is off using the verified controller profile."""

from panel_readback import PanelReadback


class AutoRunReadback(PanelReadback):
    @classmethod
    def from_profile(cls, data):
        return super().from_profile(data, {"offValue": True, "onValue": False})
