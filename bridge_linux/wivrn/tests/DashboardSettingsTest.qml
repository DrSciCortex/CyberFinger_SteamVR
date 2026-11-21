// SPDX-FileCopyrightText: 2026 DrSciCortex
//
// SPDX-License-Identifier: GPL-3.0-only

// The CyberFinger section as the WiVRn dashboard's settings page holds it (dashboard_settings_test.cpp drives it).

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as Controls
import org.kde.kirigami as Kirigami

import "../dashboard"

Kirigami.ApplicationWindow {
    id: root
    width: 760
    height: 1500
    visible: true

    property alias section: cyberfinger

    pageStack.initialPage: Kirigami.ScrollablePage {
        title: "Settings"
        ColumnLayout {
            Kirigami.FormLayout {
                id: wivrn_form
                Controls.CheckBox {
                    Kirigami.FormData.label: "A WiVRn setting above:"
                    text: "for the label column"
                }
            }
            CyberFingerSettings {
                id: cyberfinger
                twinFormLayouts: [wivrn_form]
            }
        }
    }
}
