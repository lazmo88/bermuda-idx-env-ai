"""DataUpdateCoordinator for Bermuda bluetooth data."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
import math

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import MONOTONIC_TIME, BluetoothChange, BluetoothScannerDevice
from homeassistant.components.bluetooth.api import _get_manager
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
    EventDeviceRegistryUpdatedData,
    format_mac,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import slugify
from homeassistant.util.dt import get_age, now

from .bermuda_device import BermudaDevice
from .const import (
    _LOGGER,
    _LOGGER_SPAM_LESS,
    ADDR_TYPE_PRIVATE_BLE_DEVICE,
    BDADDR_TYPE_NOT_MAC48,
    BDADDR_TYPE_PRIVATE_RESOLVABLE,
    BEACON_IBEACON_SOURCE,
    BEACON_PRIVATE_BLE_SOURCE,
    CONF_ATTENUATION,
    CONF_DEVICES,
    CONF_DEVTRACK_TIMEOUT,
    CONF_ENABLE_TRILATERATION,
    CONF_MAX_RADIUS,
    CONF_MAX_VELOCITY,
    CONF_REF_POWER,
    CONF_RSSI_OFFSETS,
    CONF_SMOOTHING_SAMPLES,
    CONF_UPDATE_INTERVAL,
    CONFDATA_SCANNERS,
    DEFAULT_ATTENUATION,
    DEFAULT_DEVTRACK_TIMEOUT,
    DEFAULT_ENABLE_TRILATERATION,
    DEFAULT_MAX_RADIUS,
    DEFAULT_MAX_VELOCITY,
    DEFAULT_REF_POWER,
    DEFAULT_SMOOTHING_SAMPLES,
    DEFAULT_UPDATE_INTERVAL,
    DEVICE_TRACKER,
    DOMAIN,
    DOMAIN_PRIVATE_BLE_DEVICE,
    HIST_KEEP_COUNT,
    PRUNE_MAX_COUNT,
    PRUNE_TIME_DEFAULT,
    PRUNE_TIME_INTERVAL,
    PRUNE_TIME_IRK,
    SIGNAL_DEVICE_NEW,
    UPDATE_INTERVAL,
)
from .util import clean_charbuf
from .trilateration import trilaterate

if TYPE_CHECKING:
    from habluetooth import BluetoothServiceInfoBleak
    from homeassistant.components.bluetooth import HomeAssistantBluetoothManager
    from homeassistant.config_entries import ConfigEntry

    from .bermuda_device_scanner import BermudaDeviceScanner

Cancellable = Callable[[], None]

class BermudaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the Bluetooth component."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self.platforms = []
        self.config_entry = entry
        self.sensor_interval = entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        self.redactions: dict[str, str] = {}
        self._redact_generic_re = re.compile(r"(?P<start>[0-9A-Fa-f]{2}):([0-9A-Fa-f]{2}:){4}(?P<end>[0-9A-Fa-f]{2})")
        self._redact_generic_sub = r"\g<start>:xx:xx:xx:xx:\g<end>"
        self.stamp_last_update: float = 0
        self.stamp_last_prune: float = 0

        # New attributes for path loss and obstruction map
        self.path_loss_factors: dict[str, float] = {}
        self.obstruction_map: dict[tuple[float, float], float] = {}
        self.fixed_beacons: list[str] = []

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self._manager: HomeAssistantBluetoothManager = _get_manager(hass)
        self.pb_state_sources: dict[str, str | None] = {}
        self.metadevices: dict[str, BermudaDevice] = {}
        self._ad_listener_cancel: Cancellable | None = None

        @callback
        def handle_state_changes(ev: Event[EventStateChangedData]):
            """Watch for new mac addresses on private ble devices and act."""
            if ev.event_type == EVENT_STATE_CHANGED:
                event_entity = ev.data.get("entity_id", "invalid_event_entity")
                if event_entity in self.pb_state_sources:
                    new_state = ev.data.get("new_state")
                    if new_state and hasattr(new_state, "attributes"):
                        new_address = new_state.attributes.get("current_address")
                        if new_address is not None and new_address.lower() != self.pb_state_sources[event_entity]:
                            _LOGGER.debug(
                                "Have a new source address for %s, %s",
                                event_entity,
                                new_address,
                            )
                            self.pb_state_sources[event_entity] = new_address.lower()
                            self._do_private_device_init = True
                            self.hass.add_job(self.async_config_entry_first_refresh())

        self.hass.bus.async_listen(EVENT_STATE_CHANGED, handle_state_changes)

        self._do_full_scanner_init = True
        self._do_private_device_init = True

        @callback
        def handle_devreg_changes(ev: Event[EventDeviceRegistryUpdatedData]):
            """Update our scanner list if the device registry is changed."""
            _LOGGER.debug(
                "Device registry has changed, we will reload scanners and Private BLE Devs. ev: %s",
                ev,
            )
            self._do_full_scanner_init = True
            self._do_private_device_init = True
            self.hass.add_job(self._async_update_data())

        hass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, handle_devreg_changes)

        self.options = {}
        self.options.update({
            CONF_ATTENUATION: DEFAULT_ATTENUATION,
            CONF_DEVTRACK_TIMEOUT: DEFAULT_DEVTRACK_TIMEOUT,
            CONF_MAX_RADIUS: DEFAULT_MAX_RADIUS,
            CONF_MAX_VELOCITY: DEFAULT_MAX_VELOCITY,
            CONF_REF_POWER: DEFAULT_REF_POWER,
            CONF_SMOOTHING_SAMPLES: DEFAULT_SMOOTHING_SAMPLES,
            CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
            CONF_RSSI_OFFSETS: {},
        })

        if hasattr(entry, "options"):
            for key, val in entry.options.items():
                if key in (
                    CONF_ATTENUATION,
                    CONF_DEVICES,
                    CONF_DEVTRACK_TIMEOUT,
                    CONF_MAX_RADIUS,
                    CONF_MAX_VELOCITY,
                    CONF_REF_POWER,
                    CONF_SMOOTHING_SAMPLES,
                    CONF_RSSI_OFFSETS,
                ):
                    self.options[key] = val

        self.devices: dict[str, BermudaDevice] = {}
        self.area_reg = ar.async_get(hass)
        self.scanner_list: list[str] = []

        if hasattr(entry, "data"):
            for address, saved in entry.data.get(CONFDATA_SCANNERS, {}).items():
                scanner = self._get_or_create_device(address)
                for key, value in saved.items():
                    if key != "options":
                        setattr(scanner, key, value)
                self.scanner_list.append(address)

        hass.services.async_register(
            DOMAIN,
            "dump_devices",
            self.service_dump_devices,
            vol.Schema(
                {
                    vol.Optional("addresses"): cv.string,
                    vol.Optional("configured_devices"): cv.boolean,
                    vol.Optional("redact"): cv.boolean,
                }
            ),
            SupportsResponse.ONLY,
        )

        if self.config_entry is not None:
            self.config_entry.async_on_unload(
                bluetooth.async_register_callback(
                    self.hass,
                    self.async_handle_advert,
                    bluetooth.BluetoothCallbackMatcher(connectable=False),
                    bluetooth.BluetoothScanningMode.ACTIVE,
                )
            )

    def calculate_path_loss_factor(self, device: BermudaDevice, scanner: BermudaDeviceScanner) -> float:
        """Calculate the path loss factor based on previously measured losses."""
        path_key = f"{device.address}_{scanner.address}"
        if path_key not in self.path_loss_factors:
            # Initialize with a default value if not present
            self.path_loss_factors[path_key] = 2.0  # Default free space path loss exponent
        return self.path_loss_factors[path_key]

    def update_path_loss_factor(self, device: BermudaDevice, scanner: BermudaDeviceScanner, measured_loss: float):
        """Update the path loss factor based on real-time measurements."""
        path_key = f"{device.address}_{scanner.address}"
        current_factor = self.path_loss_factors.get(path_key, 2.0)
        
        # Simple moving average update
        alpha = 0.1  # Adjust this value to control the update rate
        updated_factor = current_factor * (1 - alpha) + measured_loss * alpha
        
        self.path_loss_factors[path_key] = updated_factor

    def get_field_strength_estimate(self, x: float, y: float) -> float:
        """Get field strength estimate from the obstruction map."""
        # Find the nearest point in the obstruction map
        nearest_point = min(self.obstruction_map.keys(), key=lambda p: math.hypot(p[0] - x, p[1] - y))
        return self.obstruction_map[nearest_point]

    def count_wall_crossings(self, start: tuple[float, float], end: tuple[float, float]) -> int:
        """Count the number of wall crossings for a given path."""
        # Simplified implementation - assumes walls are aligned with grid lines
        wall_crossings = 0
        x1, y1 = start
        x2, y2 = end
        
        for x in range(int(min(x1, x2)), int(max(x1, x2)) + 1):
            if self.get_field_strength_estimate(x, (y1 + y2) / 2) < self.get_field_strength_estimate(x + 0.5, (y1 + y2) / 2):
                wall_crossings += 1
        
        for y in range(int(min(y1, y2)), int(max(y1, y2)) + 1):
            if self.get_field_strength_estimate((x1 + x2) / 2, y) < self.get_field_strength_estimate((x1 + x2) / 2, y + 0.5):
                wall_crossings += 1
        
        return wall_crossings

    def apply_path_loss_factor(self, device: BermudaDevice, scanner: BermudaDeviceScanner):
        """Apply the path loss factor to the calculated vector."""
        path_loss_factor = self.calculate_path_loss_factor(device, scanner)
        
        # Assuming device.position and scanner.position are tuples (x, y)
        start = device.position
        end = scanner.position
        
        # Calculate distance
        distance = math.hypot(end[0] - start[0], end[1] - start[1])
        
        # Apply path loss factor
        adjusted_distance = distance ** path_loss_factor
        
        # Count wall crossings
        wall_crossings = self.count_wall_crossings(start, end)
        
        # Adjust for wall crossings (simplified - each wall reduces signal by 50%)
        adjusted_distance *= (0.5 ** wall_crossings)
        
        # Update the device's distance to the scanner
        device.update_distance_to_scanner(scanner, adjusted_distance)

    def fine_tune_path_loss_factor(self, device: BermudaDevice):
        """Fine-tune the path loss factor using fixed beacons."""
        for beacon_address in self.fixed_beacons:
            beacon = self.devices.get(beacon_address)
            if beacon and hasattr(beacon, 'position'):
                for scanner in device.scanners.values():
                    if scanner.rssi is not None:
                        # Calculate the actual distance between the beacon and the scanner
                        actual_distance = math.hypot(beacon.position[0] - scanner.position[0],
                                                     beacon.position[1] - scanner.position[1])
                        
                        # Calculate the estimated distance based on RSSI
                        estimated_distance = self.calculate_distance_from_rssi(scanner.rssi)
                        
                        # Calculate the path loss
                        measured_loss = math.log(estimated_distance / actual_distance, 10)
                        
                        # Update the path loss factor
                        self.update_path_loss_factor(device, scanner, measured_loss)

    def calculate_distance_from_rssi(self, rssi: float) -> float:
        """Calculate distance from RSSI using the log-distance path loss model."""
        # Simplified calculation - adjust constants as needed
        txPower = -59  # Transmit power at 1 meter (adjust based on your device)
        n = 2.0  # Path loss exponent (adjust based on your environment)
        return 10 ** ((txPower - rssi) / (10 * n))

    @callback
    def async_handle_advert(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle an incoming advert callback from the bluetooth integration."""
        _LOGGER.debug(
            "New Advert! change: %s, scanner: %s mac: %s name: %s serviceinfo: %s",
            change,
            service_info.source,
            service_info.address,
            service_info.name,
            service_info,
        )
        if self.stamp_last_update < MONOTONIC_TIME() - (UPDATE_INTERVAL * 2):
            self.hass.add_job(self._async_update_data())

    def sensor_created(self, address):
        """Allows sensor platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_sensor_done = True
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")

    def device_tracker_created(self, address):
        """Allows device_tracker platform to report back that sensors have been set up."""
        dev = self._get_device(address)
        if dev is not None:
            dev.create_tracker_done = True
        else:
            _LOGGER.warning("Very odd, we got sensor_created for non-tracked device")

    def count_active_devices(self) -> int:
        """Returns the number of bluetooth devices that have recent timestamps."""
        stamp = MONOTONIC_TIME() - 10
        return sum(1 for device in self.devices.values() if device.last_seen > stamp)

    def count_active_scanners(self, max_age=10) -> int:
        """Returns count of scanners that have recently sent updates."""
        stamp = MONOTONIC_TIME() - max_age
        return sum(1 for scanner in self.get_active_scanner_summary() if scanner.get("last_stamp", 0) > stamp)

    def get_active_scanner_summary(self) -> list[dict]:
        """Returns a list of dicts suitable for seeing which scanners are configured in the system."""
        stamp = MONOTONIC_TIME()
        results = []
        for scanner in self.scanner_list:
            scannerdev = self.devices[scanner]
            last_stamp: float = 0
            for device in self.devices.values():
                record = device.scanners.get(scanner, None)
                if record is not None and record.stamp is not None:
                    if record.stamp > last_stamp:
                        last_stamp = record.stamp
            results.append(
                {
                    "name": scannerdev.name,
                    "address": scanner,
                    "last_stamp": last_stamp,
                    "last_stamp_age": stamp - last_stamp,
                }
            )
        return results

    def _get_device(self, address: str) -> BermudaDevice | None:
        """Search for a device entry based on mac address."""
        mac = format_mac(address).lower()
        return self.devices.get(mac)

    def _get_or_create_device(self, address: str) -> BermudaDevice:
        device = self._get_device(address)
        if device is None:
            mac = format_mac(address).lower()
            self.devices[mac] = device = BermudaDevice(address=mac, options=self.options)
            device.address = mac
            device.unique_id = mac
        return device

    def perform_trilateration(self, device: BermudaDevice):
        """Perform trilateration for a device if enough data is available."""
        if not self.options.get(CONF_ENABLE_TRILATERATION, DEFAULT_ENABLE_TRILATERATION):
            return

        positions = []
        distances = []
        for scanner_address, scanner in device.scanners.items():
            scanner_device = self.devices.get(scanner_address)
            if scanner.rssi_distance is not None and scanner_device and hasattr(scanner_device, 'position'):
                positions.append(scanner_device.position)
                distances.append(scanner.rssi_distance)

        if len(positions) >= 3:
            try:
                device.trilaterated_position = trilaterate(positions, distances)
                if device.trilaterated_position:
                    _LOGGER.debug(f"Trilaterated position for {device.address}: {device.trilaterated_position}")
                else:
                    _LOGGER.warning(f"Trilateration failed for {device.address}")
            except Exception as e:
                _LOGGER.error(f"Error during trilateration for {device.address}: {str(e)}")

    async def _async_update_data(self):
        """Update data for known devices by scanning bluetooth advert cache."""
        for service_info in bluetooth.async_discovered_service_info(self.hass, False):
            device = self._get_or_create_device(service_info.address)

            for company_code, man_data in service_info.advertisement.manufacturer_data.items():
                if company_code == 0x004C:  # 76 Apple Inc
                    if man_data[:2] == b"\x02\x15":  # 0x0215:  # iBeacon packet
                        device.beacon_type.add(BEACON_IBEACON_SOURCE)
                        device.beacon_uuid = man_data[2:18].hex().lower()
                        device.beacon_major = str(int.from_bytes(man_data[18:20], byteorder="big"))
                        device.beacon_minor = str(int.from_bytes(man_data[20:22], byteorder="big"))
                        device.beacon_power = int.from_bytes([man_data[22]], signed=True)
                        device.beacon_unique_id = f"{device.beacon_uuid}_{device.beacon_major}_{device.beacon_minor}"
                        device.prefname = device.beacon_unique_id
                        self.register_ibeacon_source(device)
                    else:
                        device.prefname = clean_charbuf(man_data.hex())

            if device.name is None and service_info.device.name:
                device.name = clean_charbuf(service_info.device.name)
            if device.local_name is None and service_info.advertisement.local_name:
                device.local_name = clean_charbuf(service_info.advertisement.local_name)

            device.manufacturer = device.manufacturer or service_info.manufacturer
            device.connectable = service_info.connectable

            if device.prefname is None or device.prefname.startswith(DOMAIN + "_"):
                device.prefname = device.name or device.local_name or DOMAIN + "_" + slugify(device.address)

            matched_scanners = bluetooth.async_scanner_devices_by_address(self.hass, service_info.address, False)
            for discovered in matched_scanners:
                scanner_device = self._get_device(discovered.scanner.source)
                if scanner_device is None:
                    self._do_full_scanner_init = True
                    self._do_private_device_init = True
                    self._refresh_scanners(matched_scanners)
                    scanner_device = self._get_device(discovered.scanner.source)

                if scanner_device is None:
                    _LOGGER_SPAM_LESS.error(
                        f"missing_scanner_entry_{discovered.scanner.source}",
                        "Failed to find config for scanner %s, this is probably a bug.",
                        discovered.scanner.source,
                    )
                    continue

                device.update_scanner(scanner_device, discovered)

                # Apply path loss factor and adjust for obstructions
                self.apply_path_loss_factor(device, scanner_device)

        if self.stamp_last_update == 0:
            for _source_address in self.options.get(CONF_DEVICES, []):
                self._get_or_create_device(_source_address)

        for device in self.devices.values():
            device.calculate_data()
            self.perform_trilateration(device)
            self.fine_tune_path_loss_factor(device)

        self._refresh_areas_by_min_distance()

        if self._do_full_scanner_init:
            if not self._refresh_scanners():
                _LOGGER.debug("Failed to refresh scanners, likely config entry not ready.")

        self.update_metadevices()

        for address, device in self.devices.items():
            if device.create_sensor:
                if not device.create_sensor_done or not device.create_tracker_done:
                    _LOGGER.debug("Firing device_new for %s (%s)", device.name, address)
                    async_dispatcher_send(self.hass, SIGNAL_DEVICE_NEW, address, self.scanner_list)

        if self.stamp_last_prune < MONOTONIC_TIME() - PRUNE_TIME_INTERVAL:
            self.prune_devices()
            self.stamp_last_prune = MONOTONIC_TIME()

        self.stamp_last_update = MONOTONIC_TIME()
        self.last_update_success = True

    def prune_devices(self):
        """Scan through all collected devices, and remove those that meet Pruning criteria."""
        prune_list = []
        prunable_stamps = {}

        metadevice_source_primos = set(metadevice.beacon_sources[0] for metadevice in self.metadevices.values() if metadevice.beacon_sources)

        for device_address, device in self.devices.items():
            if (
                device_address not in metadevice_source_primos
                and (not device.create_sensor)
                and (not device.is_scanner)
                and (device.last_seen > 0)
                and device.address_type != BDADDR_TYPE_NOT_MAC48
            ):
                if device.address_type == BDADDR_TYPE_PRIVATE_RESOLVABLE:
                    if device.last_seen < MONOTONIC_TIME() - PRUNE_TIME_IRK:
                        _LOGGER.debug(
                            "Marking stale IRK address for pruning: %s",
                            device.name or device_address,
                        )
                        prune_list.append(device_address)
                    else:
                        prunable_stamps[device_address] = device.last_seen
                elif device.last_seen < MONOTONIC_TIME() - PRUNE_TIME_DEFAULT:
                    _LOGGER.debug(
                        "Marking old device entry for pruning: %s",
                        device.name or device_address,
                    )
                    prune_list.append(device_address)
                else:
                    prunable_stamps[device_address] = device.last_seen

        prune_quota = len(self.devices) - len(prune_list) - PRUNE_MAX_COUNT
        if prune_quota > 0:
            sorted_addresses = sorted([(v, k) for k, v in prunable_stamps.items()])
            _LOGGER.info("Having to prune %s extra devices to make quota.", prune_quota)
            for _stamp, address in sorted_addresses[:prune_quota]:
                prune_list.append(address)

        for device_address in prune_list:
            _LOGGER.debug("Acting on prune list for %s", device_address)
            del self.devices[device_address]

    def discover_private_ble_metadevices(self):
        """Access the Private BLE Device integration to find metadevices to track."""
        entreg = er.async_get(self.hass)
        devreg = dr.async_get(self.hass)

        if self._do_private_device_init:
            self._do_private_device_init = False
            _LOGGER.debug("Refreshing Private BLE Device list")

            pb_entries = self.hass.config_entries.async_entries(DOMAIN_PRIVATE_BLE_DEVICE, include_disabled=False)
            for pb_entry in pb_entries:
                pb_entities = entreg.entities.get_entries_for_config_entry_id(pb_entry.entry_id)
                for pb_entity in pb_entities:
                    if pb_entity.domain == DEVICE_TRACKER:
                        _LOGGER.debug(
                            "Found a Private BLE Device Tracker! %s",
                            pb_entity.entity_id,
                        )

                        if pb_entity.device_id is not None:
                            pb_device = devreg.async_get(pb_entity.device_id)
                        else:
                            pb_device = None

                        pb_state = self.hass.states.get(pb_entity.entity_id)

                        if pb_state:
                            pb_source_address = pb_state.attributes.get("current_address", None)
                        else:
                            pb_source_address = None

                        _irk = pb_entity.unique_id.split("_")[0]

                        metadevice = self._get_or_create_device(_irk)
                        metadevice.create_sensor = True

                        metadevice.name = getattr(pb_device, "name_by_user", getattr(pb_device, "name", None))
                        metadevice.prefname = metadevice.name

                        if pb_entity.entity_id not in self.pb_state_sources:
                            self.pb_state_sources[pb_entity.entity_id] = None

                        if metadevice.address not in self.metadevices:
                            self.metadevices[metadevice.address] = metadevice

                        if pb_source_address is not None:
                            pb_source_address = pb_source_address.lower()

                            source_device = self._get_or_create_device(pb_source_address)
                            source_device.beacon_type.add(BEACON_PRIVATE_BLE_SOURCE)

                            if len(metadevice.beacon_sources) == 0 or metadevice.beacon_sources[0] != pb_source_address:
                                metadevice.beacon_sources.insert(0, pb_source_address)

                            self.pb_state_sources[pb_entity.entity_id] = pb_source_address

                        else:
                            _LOGGER.debug(
                                "No address available for PB Device %s",
                                pb_entity.entity_id,
                            )

    def register_ibeacon_source(self, source_device: BermudaDevice):
        """Create or update the meta-device for tracking an iBeacon."""
        if BEACON_IBEACON_SOURCE not in source_device.beacon_type:
            _LOGGER.error(
                "Only IBEACON_SOURCE devices can be used to see a beacon metadevice. %s is not.",
                source_device.name,
            )
        elif source_device.beacon_unique_id is None:
            _LOGGER.error("Source device %s is not a valid iBeacon!", source_device.name)
        else:
            metadevice = self._get_or_create_device(source_device.beacon_unique_id)
            if len(metadevice.beacon_sources) == 0:
                if metadevice.address not in self.metadevices:
                    self.metadevices[metadevice.address] = metadevice
                else:
                    _LOGGER.warning(
                        "Metadevice already tracked despite not existing yet. %s",
                        metadevice.address,
                    )

                for attribute in (
                    "beacon_unique_id",
                    "beacon_uuid",
                    "beacon_major",
                    "beacon_minor",
                    "beacon_power",
                ):
                    setattr(metadevice, attribute, getattr(source_device, attribute, None))

                if metadevice.address.upper() in self.options.get(CONF_DEVICES, []):
                    metadevice.create_sensor = True

            if source_device.address not in metadevice.beacon_sources:
                metadevice.beacon_sources.insert(0, source_device.address)
                del metadevice.beacon_sources[HIST_KEEP_COUNT:]

    def update_metadevices(self):
        """Create or update iBeacon, Private_BLE and other meta-devices from the received advertisements."""
        self.discover_private_ble_metadevices()

        for metadev in self.metadevices.values():
            latest_source: str | None = None
            source_device: BermudaDevice | None = None
            if len(metadev.beacon_sources) > 0:
                latest_source = metadev.beacon_sources[0]
                if latest_source is not None:
                    source_device = self._get_device(latest_source)

            if latest_source is not None and source_device is not None:
                metadev.scanners = source_device.scanners

                for attribute in [
                    "local_name",
                    "manufacturer",
                    "name",
                    "prefname",
                ]:
                    if hasattr(metadev, attribute):
                        if getattr(metadev, attribute) in [None, False]:
                            setattr(metadev, attribute, getattr(source_device, attribute))
                    else:
                        _LOGGER.error(
                            "Devices don't have a '%s' attribute, this is a bug.",
                            attribute,
                        )
                for attribute in [
                    "area_distance",
                    "area_id",
                    "area_name",
                    "area_rssi",
                    "area_scanner",
                    "beacon_major",
                    "beacon_minor",
                    "beacon_power",
                    "beacon_unique_id",
                    "beacon_uuid",
                    "connectable",
                    "zone",
                ]:
                    if hasattr(metadev, attribute):
                        setattr(metadev, attribute, getattr(source_device, attribute))
                    else:
                        _LOGGER.error(
                            "Devices don't have a '%s' attribute, this is a bug.",
                            attribute,
                        )

                if source_device.last_seen > metadev.last_seen:
                    metadev.last_seen = source_device.last_seen
                elif source_device.last_seen == 0:
                    pass
                elif source_device.last_seen < metadev.last_seen:
                    _LOGGER.debug(
                        "Using freshest advert from %s for %s but it's still %s seconds too old!",
                        source_device.address,
                        metadev.name,
                        metadev.last_seen - source_device.last_seen,
                    )

    def dt_mono_to_datetime(self, stamp) -> datetime:
        """Given a monotonic timestamp, convert to datetime object."""
        age = MONOTONIC_TIME() - stamp
        return now() - timedelta(seconds=age)

    def dt_mono_to_age(self, stamp) -> str:
        """Convert monotonic timestamp to age (eg: "6 seconds ago")."""
        return get_age(self.dt_mono_to_datetime(stamp))

    def _refresh_areas_by_min_distance(self):
        """Set area for ALL devices based on closest beacon."""
        for device in self.devices.values():
            if device.is_scanner is not True:
                self._refresh_area_by_min_distance(device)

    def _refresh_area_by_min_distance(self, device: BermudaDevice):
        """Very basic Area setting by finding closest beacon to a given device."""
        assert device.is_scanner is not True
        closest_scanner: BermudaDeviceScanner | None = None

        for scanner in device.scanners.values():
            if scanner.rssi_distance is not None and scanner.rssi_distance < self.options.get(
                CONF_MAX_RADIUS, DEFAULT_MAX_RADIUS
            ):
                if closest_scanner is None:
                    closest_scanner = scanner
                elif closest_scanner.rssi_distance is None or scanner.rssi_distance < closest_scanner.rssi_distance:
                    closest_scanner = scanner

        if closest_scanner is not None:
            old_area = device.area_name
            device.area_id = closest_scanner.area_id
            areas = self.area_reg.async_get_area(device.area_id)
            if hasattr(areas, "name"):
                device.area_name = getattr(areas, "name", "invalid_area")
            else:
                _LOGGER_SPAM_LESS.warning(
                    f"scanner_no_area_{closest_scanner.name}",
                    "Could not discern area from scanner %s: %s."
                    "Please assign an area then reload this integration"
                    "- Bermuda can't really work without it.",
                    closest_scanner.name,
                    areas,
                )
                device.area_name = f"No area: {closest_scanner.name}"
            device.area_distance = closest_scanner.rssi_distance
            device.area_rssi = closest_scanner.rssi
            device.area_scanner = closest_scanner.name
            if (old_area != device.area_name) and device.create_sensor:
                _LOGGER.debug(
                    "Device %s was in '%s', now in '%s'",
                    device.name,
                    old_area,
                    device.area_name,
                )
        else:
            device.area_id = None
            device.area_name = None
            device.area_distance = None
            device.area_rssi = None
            device.area_scanner = None

    def _refresh_scanners(self, scanners: list[BluetoothScannerDevice] | None = None):
        """Refresh our local (and saved) list of scanners (BLE Proxies)."""
        addresses = set()
        update_scannerlist = False
        
        if scanners is not None:
            for scanner in scanners:
                addresses.add(scanner.scanner.source.lower())

        if self._do_full_scanner_init:
            update_scannerlist = True
            for address in self.scanner_list:
                addresses.add(address.lower())
            self._do_full_scanner_init = False

        if len(addresses) > 0:
            for dev_entry in self.hass.data["device_registry"].devices.data.values():
                for dev_connection in dev_entry.connections:
                    if dev_connection[0] in ["mac", "bluetooth"]:
                        found_address = format_mac(dev_connection[1])
