// SPDX-FileCopyrightText: 2026 DrSciCortex
//
// SPDX-License-Identifier: GPL-3.0-only

// The CyberFinger driver's settings, as a section of the WiVRn dashboard's settings page: what SteamVR's settings
// show for the SteamVR driver, where WiVRn has the same, and what is WiVRn's own (the emulated controller, the
// FluxActions). The driver reads its file again when it changes, so each change here applies at once, in the
// headset too; the page's Cancel puts the file back, Reset goes back to the driver's defaults.

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

import io.github.wivrn.wivrn

Kirigami.FormLayout {
    id: cf

    property bool loading: false
    property int fluxPort: 42042 // the port to go back to when FluxActions are switched on again

    // The keys this page writes, with the driver's defaults (driver/CfConfig.h)
    readonly property var defaults: ({
        "active": true,
        "profile": "touch",
        "imu_fusion": true,
        "pose_rotation_prediction": 0.0,
        "occlusion_hold_ms": 2000,
        "release_after_ms": 2000,
        "grab_tap_to_hold": true,
        "grab_tap_ms": 200,
        "black_hold_ms": 800,
        "button_a": "STSEL",
        "button_b": "MENU",
        "button_menu": "PINK",
        "button_system": "NONE",
        "grip_offset_m": [0, 0, 0],
        "grip_angle_deg": [0, 0, 0],
        "flux_port": 42042,
        "flux_actions.a_hold": 1,
        "flux_actions.c": 3,
        "flux_actions.d": 5,
        "flux_actions.e": 7,
        "flux_actions.two_finger_point": 36,
        "flux_actions.pinky_pinch": 38,
        "flux_actions.index_point": 40,
        "flux_actions.pink_right": 42
    })

    function get(key) {
        return CyberFingerConfig.value(key, defaults[key]);
    }

    function load() {
        CyberFingerConfig.load();
        show();
    }

    function show() {
        loading = true;
        write_failed.visible = false;
        active.checked = get("active");
        profile.currentIndex = Math.max(0, profile.indexOfValue(get("profile")));
        imu_fusion.checked = get("imu_fusion");
        rotation_prediction.value = get("pose_rotation_prediction");
        occlusion_hold.value = get("occlusion_hold_ms");
        release_after.value = get("release_after_ms");
        grab_tap_to_hold.checked = get("grab_tap_to_hold");
        grab_tap.value = get("grab_tap_ms");
        black_hold.value = get("black_hold_ms");
        button_a.show(get("button_a"));
        button_b.show(get("button_b"));
        button_menu.show(get("button_menu"));
        button_system.show(get("button_system"));
        const offset = get("grip_offset_m"), angle = get("grip_angle_deg");
        offset_x.value = Math.round(offset[0] * 1000);
        offset_y.value = Math.round(offset[1] * 1000);
        offset_z.value = Math.round(offset[2] * 1000);
        angle_x.value = angle[0];
        angle_y.value = angle[1];
        angle_z.value = angle[2];
        const port = get("flux_port");
        flux.checked = port > 0;
        if (port > 0)
            fluxPort = port;
        flux_a_hold.value = get("flux_actions.a_hold");
        flux_c.value = get("flux_actions.c");
        flux_d.value = get("flux_actions.d");
        flux_e.value = get("flux_actions.e");
        flux_two_finger_point.value = get("flux_actions.two_finger_point");
        flux_pinky_pinch.value = get("flux_actions.pinky_pinch");
        flux_index_point.value = get("flux_actions.index_point");
        flux_pink_right.value = get("flux_actions.pink_right");
        loading = false;
    }

    // A control was changed: written shortly after, so a dragged slider is one write, not hundreds
    function store() {
        if (!loading && !CyberFingerConfig.unreadable)
            write.restart();
    }

    function save() {
        if (!write.running)
            return;
        write.stop();
        commit();
    }

    function commit() {
        const c = CyberFingerConfig;
        c.setValue("active", active.checked);
        c.setValue("profile", profile.currentValue);
        c.setValue("imu_fusion", imu_fusion.checked);
        c.setValue("pose_rotation_prediction", Math.round(rotation_prediction.value * 100) / 100);
        c.setValue("occlusion_hold_ms", occlusion_hold.value);
        c.setValue("release_after_ms", release_after.value);
        c.setValue("grab_tap_to_hold", grab_tap_to_hold.checked);
        c.setValue("grab_tap_ms", grab_tap.value);
        c.setValue("black_hold_ms", black_hold.value);
        c.setValue("button_a", button_a.editText.trim().toUpperCase());
        c.setValue("button_b", button_b.editText.trim().toUpperCase());
        c.setValue("button_menu", button_menu.editText.trim().toUpperCase());
        c.setValue("button_system", button_system.editText.trim().toUpperCase());
        c.setValue("grip_offset_m", [offset_x.value / 1000, offset_y.value / 1000, offset_z.value / 1000]);
        c.setValue("grip_angle_deg", [angle_x.value, angle_y.value, angle_z.value]);
        c.setValue("flux_port", flux.checked ? fluxPort : 0);
        c.setValue("flux_actions.a_hold", flux_a_hold.value);
        c.setValue("flux_actions.c", flux_c.value);
        c.setValue("flux_actions.d", flux_d.value);
        c.setValue("flux_actions.e", flux_e.value);
        c.setValue("flux_actions.two_finger_point", flux_two_finger_point.value);
        c.setValue("flux_actions.pinky_pinch", flux_pinky_pinch.value);
        c.setValue("flux_actions.index_point", flux_index_point.value);
        c.setValue("flux_actions.pink_right", flux_pink_right.value);
        write_failed.visible = !c.save();
    }

    function revert() {
        write.stop();
        CyberFingerConfig.revert();
    }

    function restoreDefaults() {
        write.stop();
        if (CyberFingerConfig.unreadable)
            return;
        CyberFingerConfig.remove(Object.keys(defaults));
        const saved = CyberFingerConfig.save();
        show();
        write_failed.visible = !saved;
    }

    Timer {
        id: write
        interval: 150
        onTriggered: cf.commit()
    }

    component Check: RowLayout {
        id: check
        property alias checked: box.checked
        property alias text: box.text
        property string help
        signal edited

        Controls.CheckBox {
            id: box
            onToggled: check.edited()
        }
        Kirigami.ContextualHelpButton {
            visible: check.help.length > 0
            toolTipText: check.help
        }
    }

    component Slide: RowLayout {
        id: slide
        property alias value: slider.value
        property alias from: slider.from
        property alias to: slider.to
        property alias stepSize: slider.stepSize
        property int decimals: 0
        property string unit
        property string zero // shown instead of 0
        property string help
        signal edited

        Controls.Slider {
            id: slider
            Layout.fillWidth: true
            Layout.minimumWidth: 200
            snapMode: Controls.Slider.SnapAlways
            onMoved: slide.edited()
        }
        Controls.Label {
            text: slider.value === 0 && slide.zero.length > 0 ? slide.zero : slider.value.toFixed(slide.decimals) + slide.unit
            horizontalAlignment: Text.AlignRight
            Layout.preferredWidth: 70
        }
        Kirigami.ContextualHelpButton {
            // kept in the row without a help text, so every slider is as wide
            opacity: slide.help.length > 0 ? 1 : 0
            enabled: slide.help.length > 0
            toolTipText: slide.help
        }
    }

    // A CyberFinger button for a controller button: one of the names, or several typed with | between them
    component ButtonChoice: Controls.ComboBox {
        id: choice
        signal edited

        editable: true
        model: ["NONE", "STSEL", "MENU", "PINK", "C", "D", "E", "JCLK"]
        onActivated: choice.edited()
        onAccepted: choice.edited()

        function show(names) {
            currentIndex = find(names);
            editText = names;
        }
    }

    component FluxNumber: Controls.SpinBox {
        id: action
        signal edited

        from: 0
        to: 42
        editable: true
        textFromValue: (value, locale) => value === 0 ? i18nc("no FluxAction for this input", "off") : value
        valueFromText: (text, locale) => parseInt(text) || 0
        onValueModified: action.edited()
    }

    Kirigami.Separator {
        Kirigami.FormData.isSection: true
    }

    Kirigami.Heading {
        text: i18n("CyberFinger")
        level: 1
        type: Kirigami.Heading.Type.Primary
    }

    Kirigami.InlineMessage {
        Layout.fillWidth: true
        visible: CyberFingerConfig.unreadable
        type: Kirigami.MessageType.Warning
        text: i18n("%1 is not plain JSON (comments?), so it is left as it is. Edit or delete it to change the settings here.", CyberFingerConfig.path)
    }

    Kirigami.InlineMessage {
        id: write_failed
        Layout.fillWidth: true
        visible: false
        type: Kirigami.MessageType.Error
        text: i18n("Could not write %1", CyberFingerConfig.path)
    }

    Controls.Label {
        Layout.maximumWidth: Kirigami.Units.gridUnit * 28
        wrapMode: Text.WordWrap
        opacity: 0.7
        text: i18n("These apply as you change them, while WiVRn runs. Cancel puts them back.")
    }

    Check {
        id: active
        enabled: !CyberFingerConfig.unreadable
        text: i18n("CyberFinger controllers active")
        help: i18n("While the bridge runs in VR Mode and the headset tracks the bare hands, each hand is a controller with the CyberFinger's buttons.\nOff: the headset's own hand tracking keeps the hands.")
        onEdited: cf.store()
    }

    ColumnLayout {
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Controller applications see:")
        Controls.ComboBox {
            id: profile
            textRole: "label"
            valueRole: "value"
            model: [
                {label: i18n("Oculus Touch"), value: "touch"},
                {label: i18n("Touch Plus (Quest 3)"), value: "touch_plus"},
                {label: i18n("Touch Pro"), value: "touch_pro"},
                {label: i18n("Valve Index"), value: "index"},
                {label: i18n("Vive Focus 3"), value: "vive_focus3"},
                {label: i18n("Pico 4"), value: "pico4"},
                {label: i18n("Pico Neo 3"), value: "pico_neo3"},
                {label: i18n("Simple controller"), value: "simple"}
            ]
            onActivated: cf.store()
        }
    }

    Check {
        id: imu_fusion
        enabled: active.checked && !CyberFingerConfig.unreadable
        text: i18n("Hand orientation from the CyberFinger's IMU")
        help: i18n("The joint IMU turns the hand, the headset keeps it aligned; also through short losses of the hand.\nIt calibrates itself on the hand in view, the first time over some tens of seconds of moving it about.\nOff: the headset's orientation alone.")
        onEdited: cf.store()
    }

    Slide {
        id: rotation_prediction
        enabled: active.checked && imu_fusion.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Rotation prediction:")
        from: 0
        to: 1
        stepSize: 0.05
        decimals: 2
        help: i18n("How much of the IMU's turning carries the orientation ahead to the time applications draw for.\n0 = none, 1 = full; above 0 the laser may jitter.")
        onEdited: cf.store()
    }

    Slide {
        id: occlusion_hold
        enabled: active.checked && imu_fusion.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Keep a lost hand in place:")
        from: 0
        to: 10000
        stepSize: 250
        unit: i18nc("milliseconds", " ms")
        zero: i18nc("a lost hand is not kept in place", "no")
        help: i18n("A hand the headset loses stays where it was this long, still turning with the IMU.")
        onEdited: cf.store()
    }

    Slide {
        id: release_after
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Give the hand back after:")
        from: 500
        to: 10000
        stepSize: 250
        unit: i18nc("milliseconds", " ms")
        help: i18n("Without reports from a CyberFinger (switched off, the bridge stopped) for this long, its hand goes back to the headset's hand tracking.")
        onEdited: cf.store()
    }

    Check {
        id: grab_tap_to_hold
        enabled: active.checked && !CyberFingerConfig.unreadable
        text: i18n("Grab: tap to hold")
        help: i18n("A quick tap of the grip holds until the next press; a longer press grabs while held.\nOff: the grab follows the grip.")
        onEdited: cf.store()
    }

    Slide {
        id: grab_tap
        enabled: active.checked && grab_tap_to_hold.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("A tap is shorter than:")
        from: 0
        to: 500
        stepSize: 10
        unit: i18nc("milliseconds", " ms")
        onEdited: cf.store()
    }

    Slide {
        id: black_hold
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Black button long press:")
        from: 0
        to: 2000
        stepSize: 50
        unit: i18nc("milliseconds", " ms")
        zero: i18nc("no long press", "none")
        help: i18n("Held this long, the black button is its long press instead (a FluxAction, below), and its own button then acts on release.\n0 = no long press.")
        onEdited: cf.store()
    }

    Kirigami.Separator {
        Kirigami.FormData.isSection: true
        Kirigami.FormData.label: i18n("CyberFinger buttons")
    }

    RowLayout {
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("A / X (lower button):")
        ButtonChoice {
            id: button_a
            onEdited: cf.store()
        }
        Kirigami.ContextualHelpButton {
            toolTipText: i18n("The CyberFinger button for the controller's lower face button.\nSTSEL: the black button; MENU: the context / rotary-dial button; PINK: the pink power button; JCLK: the stick press.\nSeveral: type them with | between, like MENU|C.")
        }
    }
    ButtonChoice {
        id: button_b
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("B / Y (upper button):")
        onEdited: cf.store()
    }
    ButtonChoice {
        id: button_menu
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Menu button (left hand):")
        onEdited: cf.store()
    }
    RowLayout {
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("System button (right hand):")
        ButtonChoice {
            id: button_system
            onEdited: cf.store()
        }
        Kirigami.ContextualHelpButton {
            toolTipText: i18n("Few applications can use the right controller's system button.")
        }
    }

    Kirigami.Separator {
        Kirigami.FormData.isSection: true
        Kirigami.FormData.label: i18n("Controller against the hand")
    }

    RowLayout {
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Offset x, y, z (mm):")
        Controls.SpinBox {
            id: offset_x
            from: -300
            to: 300
            editable: true
            onValueModified: cf.store()
        }
        Controls.SpinBox {
            id: offset_y
            from: -300
            to: 300
            editable: true
            onValueModified: cf.store()
        }
        Controls.SpinBox {
            id: offset_z
            from: -300
            to: 300
            editable: true
            onValueModified: cf.store()
        }
        Kirigami.ContextualHelpButton {
            toolTipText: i18n("Moves the controller (its grip, aim and palm poses) against the tracked hand, in the grip's frame, for applications that place the hand or a tool by the controller. The hand joints stay where the hand is.")
        }
    }
    Slide {
        id: angle_x
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Rotation X:")
        from: -180
        to: 180
        stepSize: 1
        unit: "°"
        onEdited: cf.store()
    }
    Slide {
        id: angle_y
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Rotation Y:")
        from: -180
        to: 180
        stepSize: 1
        unit: "°"
        onEdited: cf.store()
    }
    Slide {
        id: angle_z
        enabled: active.checked && !CyberFingerConfig.unreadable
        Kirigami.FormData.label: i18n("Rotation Z:")
        from: -180
        to: 180
        stepSize: 1
        unit: "°"
        onEdited: cf.store()
    }

    Kirigami.Separator {
        Kirigami.FormData.isSection: true
        Kirigami.FormData.label: i18n("FluxActions (Resonite)")
    }

    Check {
        id: flux
        enabled: active.checked && !CyberFingerConfig.unreadable
        text: i18n("Send FluxActions to Resonite")
        help: i18n("The inputs a controller has no place for go to the MoreFluxActions mod as FluxActions, for ProtoFlux on your avatar.\nThe number is the left hand's; the right hand fires the next one. off: none for that input.")
        onEdited: cf.store()
    }

    GridLayout {
        enabled: active.checked && flux.checked && !CyberFingerConfig.unreadable
        columns: 4
        columnSpacing: Kirigami.Units.largeSpacing

        Controls.Label {
            text: i18n("Black button held")
        }
        FluxNumber {
            id: flux_a_hold
            to: 41
            onEdited: cf.store()
        }
        Controls.Label {
            text: i18n("Two-finger point")
        }
        FluxNumber {
            id: flux_two_finger_point
            to: 41
            onEdited: cf.store()
        }

        Controls.Label {
            text: i18n("C")
        }
        FluxNumber {
            id: flux_c
            to: 41
            onEdited: cf.store()
        }
        Controls.Label {
            text: i18n("Pinky pinch")
        }
        FluxNumber {
            id: flux_pinky_pinch
            to: 41
            onEdited: cf.store()
        }

        Controls.Label {
            text: i18n("D")
        }
        FluxNumber {
            id: flux_d
            to: 41
            onEdited: cf.store()
        }
        Controls.Label {
            text: i18n("Index point")
        }
        FluxNumber {
            id: flux_index_point
            to: 41
            onEdited: cf.store()
        }

        Controls.Label {
            text: i18n("E")
        }
        FluxNumber {
            id: flux_e
            to: 41
            onEdited: cf.store()
        }
        Controls.Label {
            text: i18n("Right pink button")
        }
        FluxNumber {
            id: flux_pink_right
            onEdited: cf.store()
        }
    }
}
