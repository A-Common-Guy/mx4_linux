// mx4ctl cursor — reports the pointer position over D-Bus on request.
import Gio from 'gi://Gio';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const BUS_NAME = 'org.mx4ctl.Cursor';
const OBJ_PATH = '/cursor';
const IFACE = `
<node>
  <interface name="org.mx4ctl.Cursor">
    <method name="GetPosition">
      <arg type="s" direction="out" name="blob"/>
    </method>
  </interface>
</node>`;

export default class Mx4ctlCursor extends Extension {
    enable() {
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this);
        this._dbus.export(Gio.DBus.session, OBJ_PATH);
        this._owner = Gio.bus_own_name_on_connection(
            Gio.DBus.session, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, null, null);
    }

    disable() {
        if (this._owner) {
            Gio.bus_unown_name(this._owner);
            this._owner = null;
        }
        this._dbus?.unexport();
        this._dbus = null;
    }

    // "x y stage-width stage-height", logical pixels
    GetPosition() {
        const [x, y] = global.get_pointer();
        return `${x} ${y} ${global.stage.width} ${global.stage.height}`;
    }
}
