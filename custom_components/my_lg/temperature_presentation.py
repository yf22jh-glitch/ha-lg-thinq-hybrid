"""HA display step only; native temperature and command paths remain Celsius.

The standard HA climate card uses system-wide units. The separate DB-backed
display sensor provides a per-device unit without changing the wire grid.
"""


class TemperaturePresentationMixin:
    def configure_temperature_presentation(self, settings, device_id):
        self._temperature_settings = settings
        self._temperature_device_id = device_id
        self._remove_temperature_listener = None

    def _format(self):
        settings = getattr(self, '_temperature_settings', None)
        return settings.temperature_format(self._temperature_device_id) if settings else None

    @property
    def target_temperature_step(self):
        value = self._format()
        return super().target_temperature_step if value in (None, '1℉') else 0.5 if value == '0.5℃' else 1

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        settings = getattr(self, '_temperature_settings', None)
        if settings is not None:
            self._remove_temperature_listener = settings.async_add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        if getattr(self, '_remove_temperature_listener', None):
            self._remove_temperature_listener()
            self._remove_temperature_listener = None
        await super().async_will_remove_from_hass()
