# Copyright (C) 2006-2007 Red Hat, Inc.
# Copyright (C) 2009 Tomeu Vizoso, Simon Schampijer
# Copyright (C) 2009-2010 One Laptop per Child
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

from gettext import gettext as _
import logging
import sha

import dbus
import hippo
import gobject
import gtk
import gconf

from sugar.graphics.icon import CanvasIcon, Icon
from sugar.graphics.xocolor import XoColor
from sugar.graphics import xocolor
from sugar.graphics import style
from sugar.graphics.icon import get_icon_state
from sugar.graphics import palette
from sugar.graphics import iconentry
from sugar.graphics.menuitem import MenuItem
from sugar.activity.activityhandle import ActivityHandle
from sugar.activity import activityfactory
from sugar.util import unique_id
from sugar import profile

from jarabe.model import neighborhood
from jarabe.view.buddyicon import BuddyIcon
from jarabe.view.pulsingicon import CanvasPulsingIcon
from jarabe.view import launcher
from jarabe.desktop.snowflakelayout import SnowflakeLayout
from jarabe.desktop.spreadlayout import SpreadLayout
from jarabe.desktop import keydialog
from jarabe.model import bundleregistry
from jarabe.model import network
from jarabe.model import shell
from jarabe.model.network import Settings
from jarabe.model.network import IP4Config
from jarabe.model.network import WirelessSecurity
from jarabe.model.network import AccessPoint
from jarabe.model.network import OlpcMesh as OlpcMeshSettings
from jarabe.model.olpcmesh import OlpcMeshManager
from jarabe.model.adhoc import get_adhoc_manager_instance

_NM_SERVICE = 'org.freedesktop.NetworkManager'
_NM_IFACE = 'org.freedesktop.NetworkManager'
_NM_PATH = '/org/freedesktop/NetworkManager'
_NM_DEVICE_IFACE = 'org.freedesktop.NetworkManager.Device'
_NM_WIRELESS_IFACE = 'org.freedesktop.NetworkManager.Device.Wireless'
_NM_OLPC_MESH_IFACE = 'org.freedesktop.NetworkManager.Device.OlpcMesh'
_NM_ACCESSPOINT_IFACE = 'org.freedesktop.NetworkManager.AccessPoint'
_NM_ACTIVE_CONN_IFACE = 'org.freedesktop.NetworkManager.Connection.Active'

_AP_ICON_NAME = 'network-wireless'
_OLPC_MESH_ICON_NAME = 'network-mesh'

class WirelessNetworkView(CanvasPulsingIcon):
    def __init__(self, initial_ap):
        CanvasPulsingIcon.__init__(self, size=style.STANDARD_ICON_SIZE,
                                   cache=True)
        self._bus = dbus.SystemBus()
        self._access_points = {initial_ap.model.object_path: initial_ap}
        self._active_ap = None
        self._device = initial_ap.device
        self._palette_icon = None
        self._disconnect_item = None
        self._connect_item = None
        self._greyed_out = False
        self._name = initial_ap.name
        self._mode = initial_ap.mode
        self._strength = initial_ap.strength
        self._flags = initial_ap.flags
        self._wpa_flags = initial_ap.wpa_flags
        self._rsn_flags = initial_ap.rsn_flags
        self._device_caps = 0
        self._device_state = None
        self._color = None

        if self._mode == network.NM_802_11_MODE_ADHOC and \
                network.is_sugar_adhoc_network(self._name):
            self._color = profile.get_color()
        else:
            sh = sha.new()
            data = self._name + hex(self._flags)
            sh.update(data)
            h = hash(sh.digest())
            index = h % len(xocolor.colors)

            self._color = xocolor.XoColor('%s,%s' %
                                          (xocolor.colors[index][0],
                                           xocolor.colors[index][1]))

        self.connect('button-release-event', self.__button_release_event_cb)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color

        self._palette = self._create_palette()
        self.set_palette(self._palette)
        self._palette_icon.props.xo_color = self._color

        self._update_badge()

        interface_props = dbus.Interface(self._device, dbus.PROPERTIES_IFACE)
        interface_props.Get(_NM_DEVICE_IFACE, 'State',
                            reply_handler=self.__get_device_state_reply_cb,
                            error_handler=self.__get_device_state_error_cb)
        interface_props.Get(_NM_WIRELESS_IFACE, 'WirelessCapabilities',
                            reply_handler=self.__get_device_caps_reply_cb,
                            error_handler=self.__get_device_caps_error_cb)
        interface_props.Get(_NM_WIRELESS_IFACE, 'ActiveAccessPoint',
                            reply_handler=self.__get_active_ap_reply_cb,
                            error_handler=self.__get_active_ap_error_cb)

        self._bus.add_signal_receiver(self.__device_state_changed_cb,
                                      signal_name='StateChanged',
                                      path=self._device.object_path,
                                      dbus_interface=_NM_DEVICE_IFACE)
        self._bus.add_signal_receiver(self.__wireless_properties_changed_cb,
                                      signal_name='PropertiesChanged',
                                      path=self._device.object_path,
                                      dbus_interface=_NM_WIRELESS_IFACE)

    def _create_palette(self):
        icon_name = get_icon_state(_AP_ICON_NAME, self._strength)
        self._palette_icon = Icon(icon_name=icon_name,
                                  icon_size=style.STANDARD_ICON_SIZE,
                                  badge_name=self.props.badge_name)

        p = palette.Palette(primary_text=self._name,
                            icon=self._palette_icon)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        p.menu.append(self._connect_item)

        self._disconnect_item = MenuItem(_('Disconnect'), 'media-eject')
        self._disconnect_item.connect('activate',
                                        self._disconnect_activate_cb)
        p.menu.append(self._disconnect_item)

        return p

    def __device_state_changed_cb(self, new_state, old_state, reason):
        self._device_state = new_state
        self._update_state()
        self._update_badge()

    def __update_active_ap(self, ap_path):
        if ap_path in self._access_points:
            # save reference to active AP, so that we always display the
            # strength of that one
            self._active_ap = self._access_points[ap_path]
            self.update_strength()
            self._update_state()
        elif self._active_ap is not None:
            # revert to showing state of strongest AP again
            self._active_ap = None
            self.update_strength()
            self._update_state()  

    def __wireless_properties_changed_cb(self, properties):
        if 'ActiveAccessPoint' in properties:
            self.__update_active_ap(properties['ActiveAccessPoint'])

    def __get_active_ap_reply_cb(self, ap_path):
        self.__update_active_ap(ap_path)

    def __get_active_ap_error_cb(self, err):
        logging.error('Error getting the active access point: %s', err)

    def __get_device_caps_reply_cb(self, caps):
        self._device_caps = caps

    def __get_device_caps_error_cb(self, err):
        logging.error('Error getting the wireless device properties: %s', err)

    def __get_device_state_reply_cb(self, state):
        self._device_state = state
        self._update()

    def __get_device_state_error_cb(self, err):
        logging.error('Error getting the device state: %s', err)

    def _update(self):
        self._update_state()
        self._update_color()
        self._update_badge()

    def _update_state(self):
        if self._active_ap is not None:
            state = self._device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if self._mode == network.NM_802_11_MODE_ADHOC and \
                network.is_sugar_adhoc_network(self._name):
            channel = max([1] + [ap.channel for ap in
                                 self._access_points.values()])
            if state == network.DEVICE_STATE_ACTIVATED:
                icon_name = 'network-adhoc-%s-connected' % channel
            else:
                icon_name = 'network-adhoc-%s' % channel
            self.props.icon_name = icon_name
            icon = self._palette.props.icon
            icon.props.icon_name = icon_name
        else:
            if state == network.DEVICE_STATE_ACTIVATED:
                connection = network.find_connection_by_ssid(self._name)
                if connection:
                    if self._mode == network.NM_802_11_MODE_INFRA:
                        connection.set_connected()
                icon_name = '%s-connected' % _AP_ICON_NAME
            else:
                icon_name = _AP_ICON_NAME

            icon_name = get_icon_state(icon_name, self._strength)
            if icon_name:
                self.props.icon_name = icon_name
                icon = self._palette.props.icon
                icon.props.icon_name = icon_name

        if state == network.DEVICE_STATE_PREPARE or \
           state == network.DEVICE_STATE_CONFIG or \
           state == network.DEVICE_STATE_NEED_AUTH or \
           state == network.DEVICE_STATE_IP_CONFIG:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.pulsing = False
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = self._color

    def _update_badge(self):
        if self._mode != network.NM_802_11_MODE_ADHOC:
            if network.find_connection_by_ssid(self._name) is not None:
                self.props.badge_name = "emblem-favorite"
                self._palette_icon.props.badge_name = "emblem-favorite"
            elif self._flags == network.NM_802_11_AP_FLAGS_PRIVACY:
                self.props.badge_name = "emblem-locked"
                self._palette_icon.props.badge_name = "emblem-locked"
            else:
                self.props.badge_name = None
                self._palette_icon.props.badge_name = None
        else:
            self.props.badge_name = None
            self._palette_icon.props.badge_name = None

    def _disconnect_activate_cb(self, item):
        connection = network.find_connection_by_ssid(self._name)
        if connection:
            if self._mode == network.NM_802_11_MODE_INFRA:
                connection.set_disconnected()

        obj = self._bus.get_object(_NM_SERVICE, _NM_PATH)
        netmgr = dbus.Interface(obj, _NM_IFACE)

        netmgr_props = dbus.Interface(netmgr, dbus.PROPERTIES_IFACE)
        active_connections_o = netmgr_props.Get(_NM_IFACE, 'ActiveConnections')

        for conn_o in active_connections_o:
            obj = self._bus.get_object(_NM_IFACE, conn_o)
            props = dbus.Interface(obj, dbus.PROPERTIES_IFACE)
            state = props.Get(_NM_ACTIVE_CONN_IFACE, 'State')
            if state == network.NM_ACTIVE_CONNECTION_STATE_ACTIVATED:
                ap_o = props.Get(_NM_ACTIVE_CONN_IFACE, 'SpecificObject')
                if ap_o != '/' and self.find_ap(ap_o) is not None:
                    netmgr.DeactivateConnection(conn_o)
                else:
                    logging.error('Could not determine AP for'
                                  ' specific object %s' % conn_o)

    def _add_ciphers_from_flags(self, flags, pairwise):
        ciphers = []
        if pairwise:
            if flags & network.NM_802_11_AP_SEC_PAIR_TKIP:
                ciphers.append("tkip")
            if flags & network.NM_802_11_AP_SEC_PAIR_CCMP:
                ciphers.append("ccmp")
        else:
            if flags & network.NM_802_11_AP_SEC_GROUP_WEP40:
                ciphers.append("wep40")
            if flags & network.NM_802_11_AP_SEC_GROUP_WEP104:
                ciphers.append("wep104")
            if flags & network.NM_802_11_AP_SEC_GROUP_TKIP:
                ciphers.append("tkip")
            if flags & network.NM_802_11_AP_SEC_GROUP_CCMP:
                ciphers.append("ccmp")
        return ciphers

    def _get_security(self):
        if not (self._flags & network.NM_802_11_AP_FLAGS_PRIVACY) and \
                (self._wpa_flags == network.NM_802_11_AP_SEC_NONE) and \
                (self._rsn_flags == network.NM_802_11_AP_SEC_NONE):
            # No security
            return None

        if (self._flags & network.NM_802_11_AP_FLAGS_PRIVACY) and \
                (self._wpa_flags == network.NM_802_11_AP_SEC_NONE) and \
                (self._rsn_flags == network.NM_802_11_AP_SEC_NONE):
            # Static WEP, Dynamic WEP, or LEAP
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'none'
            return wireless_security

        if (self._mode != network.NM_802_11_MODE_INFRA):
            # Stuff after this point requires infrastructure
            logging.error('The infrastructure mode is not supoorted'
                          ' by your wireless device.')
            return None

        if (self._rsn_flags & network.NM_802_11_AP_SEC_KEY_MGMT_PSK) and \
                (self._device_caps & network.NM_802_11_DEVICE_CAP_RSN):
            # WPA2 PSK first
            pairwise = self._add_ciphers_from_flags(self._rsn_flags, True)
            group = self._add_ciphers_from_flags(self._rsn_flags, False)
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'wpa-psk'
            wireless_security.proto = 'rsn'
            wireless_security.pairwise = pairwise
            wireless_security.group = group
            return wireless_security

        if (self._wpa_flags & network.NM_802_11_AP_SEC_KEY_MGMT_PSK) and \
                (self._device_caps & network.NM_802_11_DEVICE_CAP_WPA):
            # WPA PSK
            pairwise = self._add_ciphers_from_flags(self._wpa_flags, True)
            group = self._add_ciphers_from_flags(self._wpa_flags, False)
            wireless_security = WirelessSecurity()
            wireless_security.key_mgmt = 'wpa-psk'
            wireless_security.proto = 'wpa'
            wireless_security.pairwise = pairwise
            wireless_security.group = group
            return wireless_security

    def __connect_activate_cb(self, icon):
        self._connect()

    def __button_release_event_cb(self, icon, event):
        self._connect()

    def _connect(self):
        connection = network.find_connection_by_ssid(self._name)
        if connection is None:
            settings = Settings()            
            settings.connection.id = 'Auto ' + self._name
            uuid = settings.connection.uuid = unique_id()
            settings.connection.type = '802-11-wireless'
            settings.wireless.ssid = self._name

            if self._mode == network.NM_802_11_MODE_INFRA:
                settings.wireless.mode = 'infrastructure'
            elif self._mode == network.NM_802_11_MODE_ADHOC:
                settings.wireless.mode = 'adhoc'
                settings.wireless.band = 'bg'
                settings.ip4_config = IP4Config()
                settings.ip4_config.method = 'link-local'

            wireless_security = self._get_security()
            settings.wireless_security = wireless_security

            if wireless_security is not None:
                settings.wireless.security = '802-11-wireless-security'

            connection = network.add_connection(uuid, settings)

        obj = self._bus.get_object(_NM_SERVICE, _NM_PATH)
        netmgr = dbus.Interface(obj, _NM_IFACE)

        netmgr.ActivateConnection(network.SETTINGS_SERVICE, connection.path,
                                  self._device.object_path,
                                  "/",
                                  reply_handler=self.__activate_reply_cb,
                                  error_handler=self.__activate_error_cb)

    def __activate_reply_cb(self, connection):
        logging.debug('Connection activated: %s', connection)

    def __activate_error_cb(self, err):
        logging.error('Failed to activate connection: %s', err)

    def set_filter(self, query):
        self._greyed_out = self._name.lower().find(query) == -1
        self._update_state()
        self._update_color()

    def create_keydialog(self, settings, response):
        keydialog.create(self._name, self._flags, self._wpa_flags,
                         self._rsn_flags, self._device_caps, settings, response)

    def update_strength(self):
        if self._active_ap is not None:
            # display strength of AP that we are connected to
            new_strength = self._active_ap.strength
        else:
            # display the strength of the strongest AP that makes up this
            # network, also considering that there may be no APs
            new_strength = max([0] + [ap.strength for ap in
                                      self._access_points.values()])

        if new_strength != self._strength:
            self._strength = new_strength
            self._update_state()

    def add_ap(self, ap):
        self._access_points[ap.model.object_path] = ap
        self.update_strength()

    def remove_ap(self, ap):
        path = ap.model.object_path
        if path not in self._access_points:
            return
        del self._access_points[path]
        if self._active_ap == ap:
            self._active_ap = None
        self.update_strength()

    def num_aps(self):
        return len(self._access_points)

    def find_ap(self, ap_path):
        if ap_path not in self._access_points:
            return None
        return self._access_points[ap_path]

    def is_olpc_mesh(self):
        return self._mode == network.NM_802_11_MODE_ADHOC \
            and self.name == "olpc-mesh"

    def remove_all_aps(self):
        for ap in self._access_points.values():
            ap.disconnect()
        self._access_points = {}
        self._active_ap = None
        self.update_strength()

    def disconnect(self):
        self._bus.remove_signal_receiver(self.__device_state_changed_cb,
                                         signal_name='StateChanged',
                                         path=self._device.object_path,
                                         dbus_interface=_NM_DEVICE_IFACE)
        self._bus.remove_signal_receiver(self.__wireless_properties_changed_cb,
                                         signal_name='PropertiesChanged',
                                         path=self._device.object_path,
                                         dbus_interface=_NM_WIRELESS_IFACE)


class SugarAdhocView(CanvasPulsingIcon):
    """To mimic the mesh behavior on devices where mesh hardware is
    not available we support the creation of an Ad-hoc network on
    three channels 1, 6, 11. This is the class for an icon
    representing a channel in the neighborhood view.

    """

    _ICON_NAME = 'network-adhoc-'
    _NAME = 'Ad-hoc Network '

    def __init__(self, channel):
        CanvasPulsingIcon.__init__(self,
                                   icon_name=self._ICON_NAME + str(channel),
                                   size=style.STANDARD_ICON_SIZE, cache=True)
        self._bus = dbus.SystemBus()
        self._channel = channel
        self._disconnect_item = None
        self._connect_item = None
        self._palette_icon = None
        self._greyed_out = False

        get_adhoc_manager_instance().connect('members-changed',
                                             self.__members_changed_cb)
        get_adhoc_manager_instance().connect('state-changed',
                                             self.__state_changed_cb)

        self.connect('button-release-event', self.__button_release_event_cb)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color
        self._state_color = XoColor('%s,%s' % \
                                       (profile.get_color().get_stroke_color(),
                                        style.COLOR_TRANSPARENT.get_svg()))
        self.props.base_color = self._state_color
        self._palette = self._create_palette()
        self.set_palette(self._palette)
        self._palette_icon.props.xo_color = self._state_color

    def _create_palette(self):
        self._palette_icon = Icon( \
                icon_name=self._ICON_NAME + str(self._channel),
                icon_size=style.STANDARD_ICON_SIZE)

        palette_ = palette.Palette(_("Ad-hoc Network %d") % self._channel,
                                   icon=self._palette_icon)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        palette_.menu.append(self._connect_item)

        self._disconnect_item = MenuItem(_('Disconnect'), 'media-eject')
        self._disconnect_item.connect('activate',
                                      self.__disconnect_activate_cb)
        palette_.menu.append(self._disconnect_item)

        return palette_

    def __button_release_event_cb(self, icon, event):
        get_adhoc_manager_instance().activate_channel(self._channel)

    def __connect_activate_cb(self, icon):
        get_adhoc_manager_instance().activate_channel(self._channel)

    def __disconnect_activate_cb(self, icon):
        get_adhoc_manager_instance().deactivate_active_channel()

    def __state_changed_cb(self, adhoc_manager, channel, device_state):
        if self._channel == channel:
            state = device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if state == network.DEVICE_STATE_ACTIVATED:
            icon_name = '%s-connected' % (self._ICON_NAME + str(self._channel))
        else:
            icon_name = self._ICON_NAME + str(self._channel)

        self.props.base_color = self._state_color
        self._palette_icon.props.xo_color = self._state_color

        if icon_name is not None:
            self.props.icon_name = icon_name
            icon = self._palette.props.icon
            icon.props.icon_name = icon_name

        if state in [network.DEVICE_STATE_PREPARE,
                     network.DEVICE_STATE_CONFIG,
                     network.DEVICE_STATE_NEED_AUTH,
                     network.DEVICE_STATE_IP_CONFIG]:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = self._state_color

    def __members_changed_cb(self, adhoc_manager, channel, has_members):
        if channel == self._channel:
            if has_members == True:
                self._state_color = profile.get_color()
                self.props.base_color = self._state_color
                self._palette_icon.props.xo_color = self._state_color
            else:
                color = '%s,%s' % (profile.get_color().get_stroke_color(),
                                   style.COLOR_TRANSPARENT.get_svg())
                self._state_color = XoColor(color)
                self.props.base_color = self._state_color
                self._palette_icon.props.xo_color = self._state_color

    def set_filter(self, query):
        name = self._NAME + str(self._channel)
        self._greyed_out = name.lower().find(query) == -1
        self._update_color()


class OlpcMeshView(CanvasPulsingIcon):
    def __init__(self, mesh_mgr, channel):
        CanvasPulsingIcon.__init__(self, icon_name=_OLPC_MESH_ICON_NAME,
                                   size=style.STANDARD_ICON_SIZE, cache=True)
        self._bus = dbus.SystemBus()
        self._channel = channel
        self._mesh_mgr = mesh_mgr
        self._disconnect_item = None
        self._connect_item = None
        self._greyed_out = False
        self._name = ''
        self._device_state = None
        self._connection = None
        self._active = False
        device = mesh_mgr.mesh_device

        self.connect('button-release-event', self.__button_release_event_cb)

        interface_props = dbus.Interface(device,
                                         'org.freedesktop.DBus.Properties')
        interface_props.Get(_NM_DEVICE_IFACE, 'State',
                            reply_handler=self.__get_device_state_reply_cb,
                            error_handler=self.__get_device_state_error_cb)
        interface_props.Get(_NM_OLPC_MESH_IFACE, 'ActiveChannel',
                            reply_handler=self.__get_active_channel_reply_cb,
                            error_handler=self.__get_active_channel_error_cb)

        self._bus.add_signal_receiver(self.__device_state_changed_cb,
                                      signal_name='StateChanged',
                                      path=device.object_path,
                                      dbus_interface=_NM_DEVICE_IFACE)
        self._bus.add_signal_receiver(self.__wireless_properties_changed_cb,
                                      signal_name='PropertiesChanged',
                                      path=device.object_path,
                                      dbus_interface=_NM_OLPC_MESH_IFACE)

        pulse_color = XoColor('%s,%s' % (style.COLOR_BUTTON_GREY.get_svg(),
                                         style.COLOR_TRANSPARENT.get_svg()))
        self.props.pulse_color = pulse_color
        self.props.base_color = profile.get_color()
        self._palette = self._create_palette()
        self.set_palette(self._palette)

    def _create_palette(self):
        _palette = palette.Palette(_("Mesh Network %d") % self._channel)

        self._connect_item = MenuItem(_('Connect'), 'dialog-ok')
        self._connect_item.connect('activate', self.__connect_activate_cb)
        _palette.menu.append(self._connect_item)

        return _palette

    def __get_device_state_reply_cb(self, state):
        self._device_state = state
        self._update()

    def __get_device_state_error_cb(self, err):
        logging.error('Error getting the device state: %s', err)

    def __device_state_changed_cb(self, new_state, old_state, reason):
        self._device_state = new_state
        self._update()

    def __get_active_channel_reply_cb(self, channel):
        self._active = (channel == self._channel)
        self._update()

    def __get_active_channel_error_cb(self, err):
        logging.error('Error getting the active channel: %s', err)

    def __wireless_properties_changed_cb(self, properties):
        if 'ActiveChannel' in properties:
            channel = properties['ActiveChannel']
            self._active = (channel == self._channel)
            self._update()

    def _update(self):
        if self._active:
            state = self._device_state
        else:
            state = network.DEVICE_STATE_UNKNOWN

        if state in [network.DEVICE_STATE_PREPARE,
                     network.DEVICE_STATE_CONFIG,
                     network.DEVICE_STATE_NEED_AUTH,
                     network.DEVICE_STATE_IP_CONFIG]:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connecting...')
            self.props.pulsing = True
        elif state == network.DEVICE_STATE_ACTIVATED:
            if self._disconnect_item:
                self._disconnect_item.show()
            self._connect_item.hide()
            self._palette.props.secondary_text = _('Connected')
            self.props.pulsing = False
        else:
            if self._disconnect_item:
                self._disconnect_item.hide()
            self._connect_item.show()
            self._palette.props.secondary_text = None
            self.props.pulsing = False

    def _update_color(self):
        if self._greyed_out:
            self.props.base_color = XoColor('#D5D5D5,#D5D5D5')
        else:
            self.props.base_color = profile.get_color()

    def __connect_activate_cb(self, icon):
        self._connect()

    def __button_release_event_cb(self, icon, event):
        self._connect()

    def _connect(self):
        self._mesh_mgr.user_activate_channel(self._channel)

    def __activate_reply_cb(self, connection):
        logging.debug('Connection activated: %s', connection)

    def __activate_error_cb(self, err):
        logging.error('Failed to activate connection: %s', err)

    def set_filter(self, query):
        self._greyed_out = (query != '')
        self._update_color()

    def disconnect(self):
        device_object_path = self._mesh_mgr.mesh_device.object_path

        self._bus.remove_signal_receiver(self.__device_state_changed_cb,
                                         signal_name='StateChanged',
                                         path=device_object_path,
                                         dbus_interface=_NM_DEVICE_IFACE)
        self._bus.remove_signal_receiver(self.__wireless_properties_changed_cb,
                                         signal_name='PropertiesChanged',
                                         path=device_object_path,
                                         dbus_interface=_NM_OLPC_MESH_IFACE)


class ActivityView(hippo.CanvasBox):
    def __init__(self, model):
        hippo.CanvasBox.__init__(self)

        self._model = model
        self._icons = {}
        self._palette = None

        self._layout = SnowflakeLayout()
        self.set_layout(self._layout)

        self._icon = self._create_icon()
        self._layout.add(self._icon, center=True)

        self._update_palette()

        activity = self._model.activity
        activity.connect('notify::name', self._name_changed_cb)
        activity.connect('notify::color', self._color_changed_cb)
        activity.connect('notify::private', self._private_changed_cb)
        activity.connect('joined', self._joined_changed_cb)
        #FIXME: 'joined' signal not working, see #5032

    def _create_icon(self):
        icon = CanvasIcon(file_name=self._model.get_icon_name(),
                    xo_color=self._model.get_color(), cache=True,
                    size=style.STANDARD_ICON_SIZE)
        icon.connect('activated', self._clicked_cb)
        return icon

    def _create_palette(self):
        p_icon = Icon(file=self._model.get_icon_name(),
                      xo_color=self._model.get_color())
        p_icon.props.icon_size = gtk.ICON_SIZE_LARGE_TOOLBAR
        p = palette.Palette(None, primary_text=self._model.activity.props.name,
                            icon=p_icon)

        private = self._model.activity.props.private
        joined = self._model.activity.props.joined

        if joined:
            item = MenuItem(_('Resume'), 'activity-start')
            item.connect('activate', self._clicked_cb)
            item.show()
            p.menu.append(item)
        elif not private:
            item = MenuItem(_('Join'), 'activity-start')
            item.connect('activate', self._clicked_cb)
            item.show()
            p.menu.append(item)

        return p

    def _update_palette(self):
        self._palette = self._create_palette()
        self._icon.set_palette(self._palette)

    def has_buddy_icon(self, key):
        return self._icons.has_key(key)

    def add_buddy_icon(self, key, icon):
        self._icons[key] = icon
        self._layout.add(icon)

    def remove_buddy_icon(self, key):
        icon = self._icons[key]
        del self._icons[key]
        icon.destroy()

    def _clicked_cb(self, item):
        shell_model = shell.get_model()
        activity = shell_model.get_activity_by_id(self._model.get_id())
        if activity:
            activity.get_window().activate(gtk.get_current_event_time())
            return

        bundle_id = self._model.get_bundle_id()
        bundle = bundleregistry.get_registry().get_bundle(bundle_id)

        launcher.add_launcher(self._model.get_id(),
                              bundle.get_icon(),
                              self._model.get_color())

        handle = ActivityHandle(self._model.get_id())
        activityfactory.create(bundle, handle)

    def set_filter(self, query):
        text_to_check = self._model.activity.props.name.lower() + \
                self._model.activity.props.type.lower()
        if text_to_check.find(query) == -1:
            self._icon.props.stroke_color = '#D5D5D5'
            self._icon.props.fill_color = style.COLOR_TRANSPARENT.get_svg()
        else:
            self._icon.props.xo_color = self._model.get_color()

        for icon in self._icons.itervalues():
            if hasattr(icon, 'set_filter'):
                icon.set_filter(query)

    def _name_changed_cb(self, activity, pspec):
        self._update_palette()

    def _color_changed_cb(self, activity, pspec):
        self._layout.remove(self._icon)
        self._icon = self._create_icon()
        self._layout.add(self._icon, center=True)
        self._icon.set_palette(self._palette)

    def _private_changed_cb(self, activity, pspec):
        self._update_palette()

    def _joined_changed_cb(self, widget, event):
        logging.debug('ActivityView._joined_changed_cb')

_AUTOSEARCH_TIMEOUT = 1000


class MeshToolbar(gtk.Toolbar):
    __gtype_name__ = 'MeshToolbar'

    __gsignals__ = {
        'query-changed': (gobject.SIGNAL_RUN_FIRST,
                          gobject.TYPE_NONE,
                          ([str]))
    }

    def __init__(self):
        gtk.Toolbar.__init__(self)

        self._query = None
        self._autosearch_timer = None

        self._add_separator()

        tool_item = gtk.ToolItem()
        self.insert(tool_item, -1)
        tool_item.show()

        self.search_entry = iconentry.IconEntry()
        self.search_entry.set_icon_from_name(iconentry.ICON_ENTRY_PRIMARY,
                                             'system-search')
        self.search_entry.add_clear_button()
        self.search_entry.set_width_chars(25)
        self.search_entry.connect('activate', self._entry_activated_cb)
        self.search_entry.connect('changed', self._entry_changed_cb)
        tool_item.add(self.search_entry)
        self.search_entry.show()

        self._add_separator(expand=True)

    def _add_separator(self, expand=False):
        separator = gtk.SeparatorToolItem()
        separator.props.draw = False
        if expand:
            separator.set_expand(True)
        else:
            separator.set_size_request(style.GRID_CELL_SIZE,
                                       style.GRID_CELL_SIZE)
        self.insert(separator, -1)
        separator.show()

    def _entry_activated_cb(self, entry):
        if self._autosearch_timer:
            gobject.source_remove(self._autosearch_timer)
        new_query = entry.props.text
        if self._query != new_query:
            self._query = new_query
            self.emit('query-changed', self._query)

    def _entry_changed_cb(self, entry):
        if not entry.props.text:
            entry.activate()
            return

        if self._autosearch_timer:
            gobject.source_remove(self._autosearch_timer)
        self._autosearch_timer = gobject.timeout_add(_AUTOSEARCH_TIMEOUT,
                                                     self._autosearch_timer_cb)

    def _autosearch_timer_cb(self):
        logging.debug('_autosearch_timer_cb')
        self._autosearch_timer = None
        self.search_entry.activate()
        return False


class DeviceObserver(gobject.GObject):
    __gsignals__ = {
        'access-point-added': (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                               ([gobject.TYPE_PYOBJECT])),
        'access-point-removed': (gobject.SIGNAL_RUN_FIRST, gobject.TYPE_NONE,
                                 ([gobject.TYPE_PYOBJECT]))
    }
    def __init__(self, device):
        gobject.GObject.__init__(self)
        self._bus = dbus.SystemBus()
        self.device = device

        wireless = dbus.Interface(device, _NM_WIRELESS_IFACE)
        wireless.GetAccessPoints(reply_handler=self._get_access_points_reply_cb,
                                 error_handler=self._get_access_points_error_cb)

        self._bus.add_signal_receiver(self.__access_point_added_cb,
                                      signal_name='AccessPointAdded',
                                      path=device.object_path,
                                      dbus_interface=_NM_WIRELESS_IFACE)
        self._bus.add_signal_receiver(self.__access_point_removed_cb,
                                      signal_name='AccessPointRemoved',
                                      path=device.object_path,
                                      dbus_interface=_NM_WIRELESS_IFACE)

    def _get_access_points_reply_cb(self, access_points_o):
        for ap_o in access_points_o:
            ap = self._bus.get_object(_NM_SERVICE, ap_o)
            self.emit('access-point-added', ap)

    def _get_access_points_error_cb(self, err):
        logging.error('Failed to get access points: %s', err)

    def __access_point_added_cb(self, access_point_o):
        ap = self._bus.get_object(_NM_SERVICE, access_point_o)
        self.emit('access-point-added', ap)

    def __access_point_removed_cb(self, access_point_o):
        self.emit('access-point-removed', access_point_o)

    def disconnect(self):
        self._bus.remove_signal_receiver(self.__access_point_added_cb,
                                         signal_name='AccessPointAdded',
                                         path=self.device.object_path,
                                         dbus_interface=_NM_WIRELESS_IFACE)
        self._bus.remove_signal_receiver(self.__access_point_removed_cb,
                                         signal_name='AccessPointRemoved',
                                         path=self.device.object_path,
                                         dbus_interface=_NM_WIRELESS_IFACE)


class NetworkManagerObserver(object):

    _SHOW_ADHOC_GCONF_KEY = '/desktop/sugar/network/adhoc'

    def __init__(self, box):
        self._box = box
        self._bus = dbus.SystemBus()
        self._devices = {}
        self._netmgr = None
        self._olpc_mesh_device_o = None

        client = gconf.client_get_default()
        self._have_adhoc_networks = client.get_bool(self._SHOW_ADHOC_GCONF_KEY)

    def listen(self):
        try:
            obj = self._bus.get_object(_NM_SERVICE, _NM_PATH)
            self._netmgr = dbus.Interface(obj, _NM_IFACE)
        except dbus.DBusException:
            logging.debug('%s service not available', _NM_SERVICE)
            return

        self._netmgr.GetDevices(reply_handler=self.__get_devices_reply_cb,
                                error_handler=self.__get_devices_error_cb)

        self._bus.add_signal_receiver(self.__device_added_cb,
                                      signal_name='DeviceAdded',
                                      dbus_interface=_NM_IFACE)
        self._bus.add_signal_receiver(self.__device_removed_cb,
                                      signal_name='DeviceRemoved',
                                      dbus_interface=_NM_IFACE)
        self._bus.add_signal_receiver(self.__properties_changed_cb,
                                      signal_name='PropertiesChanged',
                                      dbus_interface=_NM_IFACE)

        settings = network.get_settings()
        if settings is not None:
            settings.secrets_request.connect(self.__secrets_request_cb)

    def __secrets_request_cb(self, **kwargs):
        # FIXME It would be better to do all of this async, but I cannot think
        # of a good way to. NM could really use some love here.

        netmgr_props = dbus.Interface(self._netmgr, dbus.PROPERTIES_IFACE)
        active_connections_o = netmgr_props.Get(_NM_IFACE, 'ActiveConnections')

        for conn_o in active_connections_o:
            obj = self._bus.get_object(_NM_IFACE, conn_o)
            props = dbus.Interface(obj, dbus.PROPERTIES_IFACE)
            state = props.Get(_NM_ACTIVE_CONN_IFACE, 'State')
            if state == network.NM_ACTIVE_CONNECTION_STATE_ACTIVATING:
                ap_o = props.Get(_NM_ACTIVE_CONN_IFACE, 'SpecificObject')
                found = False
                if ap_o != '/':
                    for net in self._box.wireless_networks.values():
                        if net.find_ap(ap_o) is not None:
                            found = True
                            settings = kwargs['connection'].get_settings()
                            net.create_keydialog(settings, kwargs['response'])
                if not found:
                    logging.error('Could not determine AP for'
                                  ' specific object %s' % conn_o)

    def __get_devices_reply_cb(self, devices_o):
        for dev_o in devices_o:
            self._check_device(dev_o)

    def __get_devices_error_cb(self, err):
        logging.error('Failed to get devices: %s', err)

    def _check_device(self, device_o):
        device = self._bus.get_object(_NM_SERVICE, device_o)
        props = dbus.Interface(device, dbus.PROPERTIES_IFACE)

        device_type = props.Get(_NM_DEVICE_IFACE, 'DeviceType')
        if device_type == network.DEVICE_TYPE_802_11_WIRELESS:
            self._devices[device_o] = DeviceObserver(device)
            self._devices[device_o].connect('access-point-added',
                                            self.__ap_added_cb)
            self._devices[device_o].connect('access-point-removed',
                                            self.__ap_removed_cb)
            if self._have_adhoc_networks:
                self._box.add_adhoc_networks(device)
        elif device_type == network.DEVICE_TYPE_802_11_OLPC_MESH:
            self._olpc_mesh_device_o = device_o
            self._box.enable_olpc_mesh(device)

    def _get_device_path_error_cb(self, err):
        logging.error('Failed to get device type: %s', err)

    def __device_added_cb(self, device_o):
        self._check_device(device_o)

    def __device_removed_cb(self, device_o):
        if device_o in self._devices:
            observer = self._devices[device_o]
            observer.disconnect()
            del self._devices[device_o]
            if self._have_adhoc_networks:
                self._box.remove_adhoc_networks()
            return

        if self._olpc_mesh_device_o == device_o:
            self._box.disable_olpc_mesh(device_o)

    def __ap_added_cb(self, device_observer, access_point):
        self._box.add_access_point(device_observer.device, access_point)

    def __ap_removed_cb(self, device_observer, access_point_o):
        self._box.remove_access_point(access_point_o)

    def __properties_changed_cb(self, properties):
        if 'WirelessHardwareEnabled' in properties:
            if properties['WirelessHardwareEnabled']:
                if not self._have_adhoc_networks:
                    self._box.remove_adhoc_networks()
            elif properties['WirelessHardwareEnabled']:
                for device in self._devices:
                    if self._have_adhoc_networks:
                        self._box.add_adhoc_networks(device)


class MeshBox(gtk.VBox):
    __gtype_name__ = 'SugarMeshBox'

    def __init__(self):
        logging.debug("STARTUP: Loading the mesh view")

        gobject.GObject.__init__(self)

        self.wireless_networks = {}
        self._adhoc_manager = None
        self._adhoc_networks = []

        self._model = neighborhood.get_model()
        self._buddies = {}
        self._activities = {}
        self._mesh = []
        self._buddy_to_activity = {}
        self._suspended = True
        self._query = ''
        self._owner_icon = None
            
        self._toolbar = MeshToolbar()
        self._toolbar.connect('query-changed', self._toolbar_query_changed_cb)
        self.pack_start(self._toolbar, expand=False)
        self._toolbar.show()

        canvas = hippo.Canvas()
        self.add(canvas)
        canvas.show()

        self._layout_box = hippo.CanvasBox( \
                background_color=style.COLOR_WHITE.get_int())
        canvas.set_root(self._layout_box)

        self._layout = SpreadLayout()
        self._layout_box.set_layout(self._layout)

        for buddy_model in self._model.get_buddies():
            self._add_alone_buddy(buddy_model)

        self._model.connect('buddy-added', self._buddy_added_cb)
        self._model.connect('buddy-removed', self._buddy_removed_cb)
        self._model.connect('buddy-moved', self._buddy_moved_cb)

        for activity_model in self._model.get_activities():
            self._add_activity(activity_model)

        self._model.connect('activity-added', self._activity_added_cb)
        self._model.connect('activity-removed', self._activity_removed_cb)

        netmgr_observer = NetworkManagerObserver(self)
        netmgr_observer.listen()

    def do_size_allocate(self, allocation):
        width = allocation.width        
        height = allocation.height

        min_w_, icon_width = self._owner_icon.get_width_request()
        min_h_, icon_height = self._owner_icon.get_height_request(icon_width)
        x = (width - icon_width) / 2
        y = (height - icon_height) / 2 - style.GRID_CELL_SIZE
        self._layout.move(self._owner_icon, x, y)

        gtk.VBox.do_size_allocate(self, allocation)

    def _buddy_added_cb(self, model, buddy_model):
        self._add_alone_buddy(buddy_model)

    def _buddy_removed_cb(self, model, buddy_model):
        self._remove_buddy(buddy_model) 

    def _buddy_moved_cb(self, model, buddy_model, activity_model):
        # Owner doesn't move from the center
        if buddy_model.is_owner():
            return
        self._move_buddy(buddy_model, activity_model)

    def _activity_added_cb(self, model, activity_model):
        self._add_activity(activity_model)

    def _activity_removed_cb(self, model, activity_model):
        self._remove_activity(activity_model) 

    def _add_alone_buddy(self, buddy_model):
        icon = BuddyIcon(buddy_model)
        if buddy_model.is_owner():
            self._owner_icon = icon
        self._layout.add(icon)

        if hasattr(icon, 'set_filter'):
            icon.set_filter(self._query)

        self._buddies[buddy_model.get_buddy().object_path()] = icon

    def _remove_alone_buddy(self, buddy_model):
        icon = self._buddies[buddy_model.get_buddy().object_path()]
        self._layout.remove(icon)
        del self._buddies[buddy_model.get_buddy().object_path()]
        icon.destroy()

    def _remove_buddy(self, buddy_model):
        object_path = buddy_model.get_buddy().object_path()
        if self._buddies.has_key(object_path):
            self._remove_alone_buddy(buddy_model)
        else:
            for activity in self._activities.values():
                if activity.has_buddy_icon(object_path):
                    activity.remove_buddy_icon(object_path)

    def _move_buddy(self, buddy_model, activity_model):
        self._remove_buddy(buddy_model)

        if activity_model == None:
            self._add_alone_buddy(buddy_model)
        elif activity_model.get_id() in self._activities:
            activity = self._activities[activity_model.get_id()]

            icon = BuddyIcon(buddy_model, style.STANDARD_ICON_SIZE)
            activity.add_buddy_icon(buddy_model.get_buddy().object_path(), icon)

            if hasattr(icon, 'set_filter'):
                icon.set_filter(self._query)

    def _add_activity(self, activity_model):
        icon = ActivityView(activity_model)
        self._layout.add(icon)

        if hasattr(icon, 'set_filter'):
            icon.set_filter(self._query)

        self._activities[activity_model.get_id()] = icon

    def _remove_activity(self, activity_model):
        icon = self._activities[activity_model.get_id()]
        self._layout.remove(icon)
        del self._activities[activity_model.get_id()]
        icon.destroy()

    # add AP to its corresponding network icon on the desktop,
    # creating one if it doesn't already exist
    def _add_ap_to_network(self, ap):
        hash = ap.network_hash()
        if hash in self.wireless_networks:
            self.wireless_networks[hash].add_ap(ap)
        else:
            # this is a new network
            icon = WirelessNetworkView(ap)
            self.wireless_networks[hash] = icon
            self._layout.add(icon)
            if hasattr(icon, 'set_filter'):
                icon.set_filter(self._query)
 
    def _remove_net_if_empty(self, net, hash):
        # remove a network if it has no APs left
        if net.num_aps() == 0:
            net.disconnect()
            self._layout.remove(net)
            del self.wireless_networks[hash]
 
    def _ap_props_changed_cb(self, ap, old_hash):
        # if we have mesh hardware, ignore OLPC mesh networks that appear as
        # normal wifi networks
        if len(self._mesh) > 0 and ap.mode == network.NM_802_11_MODE_ADHOC \
                and ap.name == "olpc-mesh":
            logging.debug("ignoring OLPC mesh IBSS")
            ap.disconnect()
            return

        if self._adhoc_manager is not None and \
                network.is_sugar_adhoc_network(ap.name) and \
                ap.mode == network.NM_802_11_MODE_ADHOC:
            if old_hash is None: # new Ad-hoc network finished initializing
                self._adhoc_manager.add_access_point(ap)
            # we are called as well in other cases but we do not need to
            # act here as we don't display signal strength for Ad-hoc networks
            return

        if old_hash is None: # new AP finished initializing
            self._add_ap_to_network(ap)
            return

        hash = ap.network_hash()
        if old_hash == hash:
            # no change in network identity, so just update signal strengths
            self.wireless_networks[hash].update_strength()
            return

        # properties change includes a change of the identity of the network
        # that it is on. so create this as a new network.
        self.wireless_networks[old_hash].remove_ap(ap)
        self._remove_net_if_empty(self.wireless_networks[old_hash], old_hash)
        self._add_ap_to_network(ap)

    def add_access_point(self, device, ap_o):
        ap = AccessPoint(device, ap_o)
        ap.connect('props-changed', self._ap_props_changed_cb)
        ap.initialize()

    def remove_access_point(self, ap_o):
        if self._adhoc_manager is not None:
            if self._adhoc_manager.is_sugar_adhoc_access_point(ap_o):
                self._adhoc_manager.remove_access_point(ap_o)
                return

        # we don't keep an index of ap object path to network, but since
        # we'll only ever have a handful of networks, just try them all...
        for net in self.wireless_networks.values():
            ap = net.find_ap(ap_o)
            if not ap:
                continue

            ap.disconnect()
            net.remove_ap(ap)
            self._remove_net_if_empty(net, ap.network_hash())
            return

        # it's not an error if the AP isn't found, since we might have ignored
        # it (e.g. olpc-mesh adhoc network)
        logging.debug('Can not remove access point %s' % ap_o)

    def add_adhoc_networks(self, device):
        if self._adhoc_manager is None:
            self._adhoc_manager = get_adhoc_manager_instance()
            self._adhoc_manager.start_listening(device)
        self._add_adhoc_network_icon(1)
        self._add_adhoc_network_icon(6)
        self._add_adhoc_network_icon(11)
        self._adhoc_manager.autoconnect()

    def remove_adhoc_networks(self):
        for icon in self._adhoc_networks:
            self._layout.remove(icon)
        self._adhoc_networks = []

    def _add_adhoc_network_icon(self, channel):
        icon = SugarAdhocView(channel)
        self._layout.add(icon)
        self._adhoc_networks.append(icon)

    def _add_olpc_mesh_icon(self, mesh_mgr, channel):
        icon = OlpcMeshView(mesh_mgr, channel)
        self._layout.add(icon)
        self._mesh.append(icon)

    def enable_olpc_mesh(self, mesh_device):
        mesh_mgr = OlpcMeshManager(mesh_device)
        self._add_olpc_mesh_icon(mesh_mgr, 1)
        self._add_olpc_mesh_icon(mesh_mgr, 6)
        self._add_olpc_mesh_icon(mesh_mgr, 11)

        # the OLPC mesh can be recognised as a "normal" wifi network. remove
        # any such normal networks if they have been created
        for hash, net in self.wireless_networks.iteritems():
            if not net.is_olpc_mesh():
                continue

            logging.debug("removing OLPC mesh IBSS")
            net.remove_all_aps()
            net.disconnect()
            self._layout.remove(net)
            del self.wireless_networks[hash]

    def disable_olpc_mesh(self, mesh_device):
        for icon in self._mesh:
            icon.disconnect()
            self._layout.remove(icon)
        self._mesh = []

    def suspend(self):
        if not self._suspended:
            self._suspended = True
            for net in self.wireless_networks.values() + self._mesh:
                net.props.paused = True

    def resume(self):
        if self._suspended:
            self._suspended = False
            for net in self.wireless_networks.values() + self._mesh:
                net.props.paused = False

    def _toolbar_query_changed_cb(self, toolbar, query):
        self._query = query.lower()
        for icon in self._layout_box.get_children():
            if hasattr(icon, 'set_filter'):
                icon.set_filter(self._query)

    def focus_search_entry(self):
        self._toolbar.search_entry.grab_focus()
