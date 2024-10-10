"""Adds config flow for Bermuda BLE Trilateration."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import MONOTONIC_TIME, BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigEntry, OptionsFlowWithConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    ObjectSelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    ADDR_TYPE_IBEACON,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_PRIVATE_RESOLVABLE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_ENABLE_TRILATERATION,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SAVE_AND_CLOSE,
    CONF_SCANNER_INFO,
    CONF_SCANNERS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_ENABLE_TRILATERATION,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    NAME,
)
from .util import rssi_to_metres

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.data_entry_flow import FlowResult

    from .bermuda_device import BermudaDevice
    from .coordinator import BermudaDataUpdateCoordinator


class BermudaFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for bermuda."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._errors = {}

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> FlowResult:
        """Handle a flow initialized by bluetooth discovery."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return self.async_show_form(step_id="user", description_placeholders={"name": NAME})

    async def async_step_user(self, user_input=None):
        """Handle a flow initialized by the user."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title=NAME, data={"source": "user"}, description=NAME)

        return self.async_show_form(step_id="user", description_placeholders={"name": NAME})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BermudaOptionsFlowHandler(config_entry)


class BermudaOptionsFlowHandler(OptionsFlowWithConfigEntry):
    """Config flow options handler for bermuda."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize HACS options flow."""
        super().__init__(config_entry)
        self.coordinator: BermudaDataUpdateCoordinator
        self.devices: dict[str, BermudaDevice]
        self._last_ref_power = None
        self._last_device = None
        self._last_scanner = None
        self._last_attenuation = None
        self._last_scanner_info = None

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        self.coordinator = self.hass.data[DOMAIN][self.config_entry.entry_id]
        self.devices = self.coordinator.devices

        return self.async_show_menu(
            step_id="init",
            menu_options={
                "globalopts": "Global Options",
                "selectdevices": "Select Devices",
                "calibration1_global": "Calibration 1: Global",
                "calibration2_scanners": "Calibration 2: Scanner RSSI Offsets",
            },
        )

    async def async_step_globalopts(self, user_input=None):
        """Handle global options flow."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        data_schema = {
            vol.Required(
                CONF_MAX_RADIUS,
                default=self.options.get(CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS),
            ): vol.Coerce(float),
            vol.Required(
                CONF_MAX_VELOCITY,
                default=self.options.get(CONF_MAX_VELOCITY, DEFAULT_MAX_VELOCITY),
            ): vol.Coerce(float),
            vol.Required(
                CONF_DEVTRACK_TIMEOUT,
                default=self.options.get(CONF_DEVTRACK_TIMEOUT, DEFAULT_DEVTRACK_TIMEOUT),
            ): vol.Coerce(int),
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=self.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
            ): vol.Coerce(float),
            vol.Required(
                CONF_SMOOTHING_SAMPLES,
                default=self.options.get(CONF_SMOOTHING_SAMPLES, DEFAULT_SMOOTHING_SAMPLES),
            ): vol.Coerce(int),
            vol.Required(
                CONF_ATTENUATION,
                default=self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            ): vol.Coerce(float),
            vol.Required(
                CONF_REF_POWER,
                default=self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
            ): vol.Coerce(float),
            vol.Required(
                CONF_ENABLE_TRILATERATION,
                default=self.options.get(CONF_ENABLE_TRILATERATION, DEFAULT_ENABLE_TRILATERATION),
            ): bool,
        }

        return self.async_show_form(step_id="globalopts", data_schema=vol.Schema(data_schema))

    async def async_step_selectdevices(self, user_input=None):
        """Handle device selection flow."""
        if user_input is not None:
            self.options.update(user_input)
            return await self._update_options()

        options_list = []
        options_metadevices = []
        options_otherdevices = []
        options_randoms = []

        for device in self.devices.values():
            name = device.prefname or device.name or ""

            if device.is_scanner:
                continue
            if device.address_type == ADDR_TYPE_PRIVATE_BLE_DEVICE:
                continue
            if device.address_type == ADDR_TYPE_IBEACON:
                source_mac = f"[{device.beacon_sources[0].upper()}]" if device.beacon_sources else ""
                options_metadevices.append(
                    SelectOptionDict(
                        value=device.address.upper(),
                        label=f"iBeacon: {device.address.upper()} {source_mac} "
                        f"{name if device.address.upper() != name.upper() else ''}",
                    )
                )
                continue

            if device.address_type == BDADDR_TYPE_PRIVATE_RESOLVABLE:
                if device.last_seen < MONOTONIC_TIME() - (60 * 60 * 2):
                    continue
                options_randoms.append(
                    SelectOptionDict(
                        value=device.address.upper(),
                        label=f"[{device.address.upper()}] {name} (Random MAC)",
                    )
                )
                continue

            options_otherdevices.append(
                SelectOptionDict(
                    value=device.address.upper(),
                    label=f"[{device.address.upper()}] {name}",
                )
            )

        options_metadevices.sort(key=lambda item: item["label"])
        options_otherdevices.sort(key=lambda item: item["label"])
        options_randoms.sort(key=lambda item: item["label"])
        options_list.extend(options_metadevices)
        options_list.extend(options_otherdevices)
        options_list.extend(options_randoms)

        for address in self.options.get(CONF_DEVICES, []):
            if not next(
                (item for item in options_list if item["value"] == address.upper()),
                False,
            ):
                options_list.append(SelectOptionDict(value=address.upper(), label=f"[{address}] (saved)"))

        data_schema = {
            vol.Optional(
                CONF_DEVICES,
                default=self.options.get(CONF_DEVICES, []),
            ): SelectSelector(SelectSelectorConfig(options=options_list, multiple=True)),
        }

        return self.async_show_form(step_id="selectdevices", data_schema=vol.Schema(data_schema))

    async def async_step_calibration1_global(self, user_input=None):
        """Handle global calibration flow."""
        if user_input is not None:
            if user_input[CONF_SAVE_AND_CLOSE]:
                self.options.update(
                    {
                        CONF_ATTENUATION: user_input[CONF_ATTENUATION],
                        CONF_REF_POWER: user_input[CONF_REF_POWER],
                    }
                )
                return await self._update_options()

            self._last_ref_power = user_input[CONF_REF_POWER]
            self._last_attenuation = user_input[CONF_ATTENUATION]
            self._last_device = user_input[CONF_DEVICES]
            self._last_scanner = user_input[CONF_SCANNERS]

        scanner_options = [
            SelectOptionDict(
                value=scanner,
                label=self.coordinator.devices[scanner].name if scanner in self.coordinator.devices else scanner,
            )
            for scanner in self.coordinator.scanner_list
        ]
        data_schema = {
            vol.Required(
                CONF_DEVICES,
                default=self._last_device if self._last_device is not None else vol.UNDEFINED,
            ): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
            vol.Required(
                CONF_SCANNERS,
                default=self._last_scanner if self._last_scanner is not None else vol.UNDEFINED,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=scanner_options,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_REF_POWER,
                default=self._last_ref_power
                if self._last_ref_power is not None
                else self.options.get(CONF_REF_POWER, DEFAULT_REF_POWER),
            ): vol.Coerce(float),
            vol.Required(
                CONF_ATTENUATION,
                default=self._last_attenuation
                if self._last_attenuation is not None
                else self.options.get(CONF_ATTENUATION, DEFAULT_ATTENUATION),
            ): vol.Coerce(float),
            vol.Optional(CONF_SAVE_AND_CLOSE, default=False): vol.Coerce(bool),
        }

        return self.async_show_form(
            step_id="calibration1_global",
            data_schema=vol.Schema(data_schema),
        )

    async def async_step_calibration2_scanners(self, user_input=None):
        """Handle scanner RSSI offsets calibration flow."""
        if user_input is not None:
            if user_input[CONF_SAVE_AND_CLOSE]:
                rssi_offset_by_address = {}
                for address in self.coordinator.scanner_list:
                    scanner_name = self.coordinator.devices[address].name
                    rssi_offset_by_address[address] = user_input[CONF_SCANNER_INFO][scanner_name]

                self.options.update({CONF_RSSI_OFFSETS: rssi_offset_by_address})
                return await self._update_options()

            self._last_scanner_info = user_input[CONF_SCANNER_INFO]
            self._last_device = user_input[CONF_DEVICES]

        saved_rssi_offsets = self.options.get(CONF_RSSI_OFFSETS, {})
        rssi_offset_dict = {}

        for scanner in self.coordinator.scanner_list:
            scanner_name = self.coordinator.devices[scanner].name
            rssi_offset_dict[scanner_name] = saved_rssi_offsets.get(scanner, 0)
        data_schema = {
            vol.Required(
                CONF_DEVICES,
                default=self._last_device if self._last_device is not None else vol.UNDEFINED,
            ): DeviceSelector(DeviceSelectorConfig(integration=DOMAIN)),
            vol.Required(
                CONF_SCANNER_INFO,
                default=rssi_offset_dict if not self._last_scanner_info else self._last_scanner_info,
            ): ObjectSelector(),
            vol.Optional(CONF_SAVE_AND_CLOSE, default=False): vol.Coerce(bool),
        }

        return self.async_show_form(
            step_id="calibration2_scanners",
            data_schema=vol.Schema(data_schema),
        )

    async def _update_options(self):
        """Update config entry options."""
        return self.async_create_entry(title=NAME, data=self.options)
