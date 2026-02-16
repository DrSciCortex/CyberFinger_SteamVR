/*
 * SPDX-FileCopyrightText: 2026 DrSciCortex
 *
 * SPDX-License-Identifier: GPL-3.0-only
 */
// ═══════════════════════════════════════════════════════════════════════════
// ServerProvider.cpp — Main SteamVR driver provider
// ═══════════════════════════════════════════════════════════════════════════

#include "ServerProvider.h"
#include "Utils.h"

namespace merged_ctrl {

vr::EVRInitError ServerProvider::Init(vr::IVRDriverContext* pDriverContext) {
    VR_INIT_SERVER_DRIVER_CONTEXT(pDriverContext);

    SetDriverLog(vr::VRDriverLog());
    DriverLog("═══ Merged Controller Driver v1.0 ═══\n");
    DriverLog("Initializing...\n");

    // ── Read settings ──────────────────────────────────────────────────
    const char* section = "driver_merged_controller";

    std::string serialL = GetDriverSettingString(section, "serialNumber_left", "MERGED_CTRL_L");
    std::string serialR = GetDriverSettingString(section, "serialNumber_right", "MERGED_CTRL_R");

    int udpPort  = GetDriverSettingInt(section, "handtracking_udp_port", 27015);

    DriverLog("  Left  serial: %s\n", serialL.c_str());
    DriverLog("  Right serial: %s\n", serialR.c_str());
    DriverLog("  UDP port: %d\n", udpPort);

    // ── Start subsystems ───────────────────────────────────────────────
    m_handTrackingReceiver = std::make_unique<HandTrackingReceiver>();
    m_handTrackingReceiver->Start(udpPort);

    // ── Create controller devices ──────────────────────────────────────
    m_leftController = std::make_unique<MergedController>(
        0, serialL, m_handTrackingReceiver.get());
    m_rightController = std::make_unique<MergedController>(
        1, serialR, m_handTrackingReceiver.get());

    // Register with SteamVR
    vr::VRServerDriverHost()->TrackedDeviceAdded(
        serialL.c_str(), vr::TrackedDeviceClass_Controller, m_leftController.get());
    vr::VRServerDriverHost()->TrackedDeviceAdded(
        serialR.c_str(), vr::TrackedDeviceClass_Controller, m_rightController.get());

    DriverLog("Merged Controller Driver initialized OK\n");
    return vr::VRInitError_None;
}

void ServerProvider::Cleanup() {
    DriverLog("Merged Controller Driver shutting down\n");

    m_leftController.reset();
    m_rightController.reset();
    m_handTrackingReceiver.reset();

    VR_CLEANUP_SERVER_DRIVER_CONTEXT();
}

const char* const* ServerProvider::GetInterfaceVersions() {
    return vr::k_InterfaceVersions;
}

void ServerProvider::RunFrame() {
    if (m_leftController)  m_leftController->Update();
    if (m_rightController) m_rightController->Update();
}

bool ServerProvider::ShouldBlockStandbyMode() { return false; }
void ServerProvider::EnterStandby() {}
void ServerProvider::LeaveStandby() {}

} // namespace merged_ctrl

